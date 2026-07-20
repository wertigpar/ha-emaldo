cd /home/mikko/wertigpar/ha-emaldo-repo/custom_components/emaldo
python3 -m py_compile config_flow.py schedule_coordinator.py const.py && echo PY_OK
for f in strings.json manifest.json translations/da.json translations/nb.json translations/fi.json translations/sv.json; do
  python3 -c "import json,sys; json.load(open(sys.argv[1])); print('OK', sys.argv[1])" "$f"
done
