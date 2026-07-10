# EMALDO Architecture

## Account Hierarchy

```
Emaldo User Account (email + password)
  │
  ├── App credentials (app_id, app_secret)
  │
  ├── HOME A (home_id_A) ───────────────────────── ...
  │   │
  │   ├── DEVICE 1 ─── Battery Modules (slot 0, 1, …)
  │   │   (device_id_1, model, chat_secret_1)
  │   │   ├── Power Core / Power Store
  │   │   └── Extension cabinets (extra batteries)
  │   │
  │   ├── DEVICE 2
  │   │   (device_id_2, model, chat_secret_2)
  │   │   └── Power Store
  │   │
  │   └── ...
  │
  ├── HOME B (home_id_B) ───────────────────────── ...
  │   │
  │   └── (no devices, or devices)
  │
  └── ...
```

A user account can have **multiple homes** (e.g. a real home plus a virtual home
created when inviting another user). Devices live under one home; having devices
in multiple homes is rare but possible. Each home has its own `home_id`, and
devices are uniquely identified by `(home_id, device_id)`.

## Sessions

### REST Session (HTTPS)

```
  HA (config entry)
    │
    EmaldoCoordinator ─── SharedEmaldoClient ─── api.emaldo.com
    │   (slow, 60s)           │                     (HTTPS 443)
    │                          │
    │                          └── Cached auth token (shared across devices)
    │
    EmaldoRealtimeCoordinator ── get_e2e_credentials()
        (per device)
```

- **One REST session** per config entry, shared by all devices.
- The `SharedEmaldoClient` caches the auth token so per-device `e2e_login` calls
  reuse the same home-level login without rotating the shared home secret.
- Credentials are cached with a 10-minute TTL; force-refresh on confirmed failures.

### E2E Session (UDP)

**Current (beta15i) architecture — shared session, reverse-engineered from relay
behavior:**

```
  HA ─── EmaldoRealtimeCoordinator (primary, is_primary=True)
           │
           │ owns
           ▼
         PersistentE2ESession ───── e2e3.emaldo.com:1050  (UDP)
           │                           │
           │  Socket + stream thread   │  push frames from ALL devices
           │  + keepalive + drain      │  in the home arrive here
           │                           │
           │  _device_key_registry      │
           │  {home_id:                │
           │    {device_id_1: secret_1,│
           │     device_id_2: secret_2}}│
           │                           │
           │  Drain loop:              │
           │    recvfrom() ────────────┤
           │      ↓                    │
           │    Try own secret ── OK → cache(device_id_1, data)
           │      ↓ fail               │
           │    Try alt secret ── OK → cache(device_id_2, data)
           │                           │
           └── Shared by ──────────────┘
                 │
           EmaldoRealtimeCoordinator (secondary, is_primary=False)
                 │
                 └── Reads cached frame via get_latest_power_flow(device_id=...)
                     Never sends Alive(home), never closes session.
```

**Flow detail for multi-device:**

```
Relay pushes encrypted 0x30 frames for ALL devices of this home
to EVERY open session socket. The primary's stream receives frames
from both PS1 and PS2 interleaved.

Frame from PS1:
  recvfrom() → try own chat_secret (PS1's secret) → decrypt OK
  → store under device_id=PS1 → continue

Frame from PS2:
  recvfrom() → try own chat_secret (PS1's secret) → decrypt fails
  → try alt chat_secret (PS2's secret, from _device_key_registry)
  → decrypt OK → store under device_id=PS2 → continue

Each coordinator reads only its own device's cached frame:
  PS1 coordinator: get_latest_power_flow(device_id=PS1) → PS1 data
  PS2 coordinator: get_latest_power_flow(device_id=PS2) → PS2 data
```

**Sessions and credentials detail:**

```
┌──────────────────────────────────────────────────────────┐
│                   E2E Credentials per Device              │
│                                                          │
│  {                                                        │
│    "host":               "e2e3.emaldo.com",               │
│    "port":               1050,                            │
│    "home_end_id":        shared across all devices,       │
│    "home_group_id":      shared across all devices,       │
│    "home_end_secret":    shared across all devices,       │
│    "sender_end_id":      unique per device,               │
│    "sender_group_id":    unique per device,               │
│    "sender_end_secret":  unique per device,               │
│    "chat_secret":        UNIQUE per device — changes      │
│                          on every e2e_login call          │
│  }                                                        │
│                                                          │
│  home_* fields identify the HOME to the relay.            │
│  sender_* + chat_secret identify the DEVICE.             │
│                                                          │
│  One session socket → one handshake with one device's     │
│  creds. But the relay pushes frames for ALL devices on    │
│  that home to the socket. The drain loop tries each       │
│  registered chat_secret until one decrypts the packet.    │
└──────────────────────────────────────────────────────────┘
```

### Important Notes

- **Keepalive does NOT refresh the 0x30 subscription TTL** — only 0x30
  subscribe/read packets reset the relay's per-device power-flow expiry timer.
  The timer is ~7-8s, so the stream resubscribes every 12s (above the relay's
  ~10s spacing wall) and bridges gaps via a 28s stale window.
- **chat_secret rotation** — each `e2e_login` call rotates the per-device
  `chat_secret` server-side. If the stream re-handshakes with a stale secret,
  decryption fails (undecryptable packets). The `creds_provider` callback
  fetches current creds on reconnect to avoid this death spiral.
- **Single session, single socket** — the primary device owns the UDP socket and
  stream thread. Secondary devices read the cached frame and never open their
  own socket, never send `Alive(home)`, never start their own keepalive loop.
  This prevents the relay collision that occurred when two devices on the same
  home both sent `Alive(home)` repeatedly.

## HA Integration Structure

```
Config Entry (one per HA, one per Emaldo account)
  │
  ├── EmaldoCoordinator (slow REST, 60s interval)
  │     └── SharedEmaldoClient (shared auth token)
  │
  ├── devices[0] ─────────────────────────────────────
  │   ├── EmaldoRealtimeCoordinator (is_primary=True)
  │   │     └── PersistentE2ESession (owns socket + stream)
  │   └── EmaldoScheduleCoordinator
  │
  ├── devices[1] ─────────────────────────────────────
  │   ├── EmaldoRealtimeCoordinator (is_primary=False)
  │   │     └── Shared session (read-only, no Alive)
  │   └── EmaldoScheduleCoordinator
  │
  └── ...
```

The `__init__.py` discovers all devices under the account's home during setup.
The first-discovered device becomes the **primary** (owns the E2E session).
Subsequent devices are **secondary** (share the primary's session).

## Battery Modules

```
Device (Power Store / Power Core)
  │
  ├── Slot 0 ─── Battery Module (serial, soc, temperature, …)
  ├── Slot 1 ─── Battery Module
  ├── Slot 2 ─── (empty)
  ├── Slot 3 ─── Battery Module (extension cabinet)
  └── ...

Each module is probed via the E2E session during a ~5-minute battery scan.
Module data is cached per slot and survives transient probe failures.
Extension cabinets appear as additional slots within the owning device —
they are NOT separate HA devices.
```
