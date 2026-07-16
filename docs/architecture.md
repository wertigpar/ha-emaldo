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
- Two-tier credential caching: per-device credentials (`e2e_login`) cached **10
  minutes**, account-level home login (`/home/e2e-login/`) cached **30 minutes**
  with a per-home serialization lock to prevent concurrent rotation on
  multi-device accounts (#47). Force-refresh on confirmed failures via
  `_needs_fresh_creds`.

### E2E Session (UDP)

**Current (beta16) architecture — shared session, reverse-engineered from relay
behavior:**

```
  HA ─── EmaldoRealtimeCoordinator (primary, is_primary=True)
           │
           │ owns
           ▼
         PersistentE2ESession ───── e2e2.emaldo.com:1050  (UDP)
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

**Flow detail for multi-device (per-device sessions, #47 Option C / beta16h-C3):**

```
Each device creates its OWN PersistentE2ESession (its own UDP socket,
established as itself via its own Alive(home)+Alive(device)+Wake+Heartbeat).
The relay pushes 0x30 frames for ALL devices of the home to EVERY open
session socket, so each device's stream also receives the OTHER device's
frames — but each session decrypts only its own device's frames (the other
device's packets fail decryption and are ignored / identified as non-data
pushes).

PS1 session socket:
  recvfrom() → try own chat_secret (PS1) → decrypt OK → cache(PS1)
  recvfrom() → try own chat_secret (PS1) → PS2 frame decrypt FAILS
               → ignored (or classified as status/control push)

PS2 session socket:
  recvfrom() → try own chat_secret (PS2) → decrypt OK → cache(PS2)

Each coordinator reads only its own device's cached frame:
  PS1 coordinator: get_latest_power_flow(device_id=PS1) → PS1 data
  PS2 coordinator: get_latest_power_flow(device_id=PS2) → PS2 data

Commands: a device sends through its OWN session. If a command must be
routed to a different device (cross-device), the session merges the TARGET
device's sender_end_id/groupId/chat_secret into the wire packet via
send_command_for_creds() while still addressing it by recipient_end_id.
```

**Sessions and credentials detail:**

```
┌──────────────────────────────────────────────────────────┐
│              E2E Credentials per Device (Session)         │
│                                                          │
│  {                                                        │
│    "host":               "e2e2.emaldo.com" (or API-      │
│                          returned, with e2e2 fallback),  │
│    "port":               1050,                            │
│    "home_end_id":        shared across all devices,       │
│    "home_group_id":      shared across all devices,       │
│    "home_end_secret":    shared across all devices,       │
│    "home_chat_secret":   shared across all devices        │
│                          (fallback for power-flow decrypt)│
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
│  One session socket per device → one handshake per device│
│  with that device's own creds. The relay nonetheless      │
│  pushes frames for all home devices to every socket; each│
│  session decrypts only its own device's frames.           │
│                                                          │
│  Home-secret coordination (#47 C3): the PRIMARY publishes │
│  home_end_secret/home_chat_secret to a shared dict at     │
│  session creation; SECONDARY overrides its fetched home   │
│  secret with the primary's value and NEVER rotates it     │
│  (allow_home_refresh=False). This breaks the dual-unit    │
│  21204 ping-pong.                                         │
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
  **Important:** commands sent through the shared session must use each device's
  own ``chat_secret`` / ``sender_end_id`` — use
  ``send_command_for_creds(msg_type, payload, own_creds)`` instead of
  ``send_command(msg_type, payload)``, which otherwise encrypts with the session
  owner's credentials and routes the command to the wrong device.
- **Per-home E2E credential lock (#47)** — ``/home/e2e-login/`` rotates the
  shared ``home_end_secret`` server-side on every call. A per-home
  ``threading.Lock`` serializes concurrent calls so two devices on the same
  account cannot race and rotate the secret back and forth. The result is cached
  for 30 minutes; a 5-second grace window reuses the cache when
  ``force_refresh`` is requested within that window after a previous refresh.
- **Home secret rotation callbacks (#47)** — ``_get_home_e2e()`` fires
  registered callbacks after a fresh ``/home/e2e-login/`` so live sessions can
  re-key their in-memory credentials (``home_end_id``, ``home_group_id``,
  ``home_end_secret``, ``home_chat_secret``) via ``rekey_home()`` without a UDP
  re-handshake. Callbacks are fired outside the per-home lock to avoid deadlock.
- **Force-logout detection (#47)** — the relay may send an encrypted JSON
  datagram ``{"cmd":"force-logout"}`` when another device rotates the home
  ``end_secret``. The stream drain loop decrypts with ``home_end_secret``,
  flags a reconnect with forced credential refresh, and the session re-keys via
  the rotation callback on the next handshake.
- **Callback cleanup** — ``_invalidate_session_ref()`` calls the unregister
  callable returned by ``register_home_secret_callback()`` so an old session is
  not re-keyed after the coordinator has moved on to a replacement session.
- **Thread safety** — ``_ensure_session()`` sets ``_session_binding`` before
  entering the frame-wait loop and uses a local ``_session`` reference inside
  the loop, so a concurrent thread cannot set ``self._session`` to ``None``
  between the check and the use (#47 RC5).
- **Legacy fallback mode** — if the persistent stream delivers zero usable power
  flow frames (restrictive NAT/firewall dropping device push datagrams), the
  coordinator falls back to beta9-style one-shot reads: fresh socket, full
  handshake, single 0x30 read, socket teardown per poll cycle.

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
