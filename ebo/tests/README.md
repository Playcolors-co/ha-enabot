# Test suite — EBO Air 2 add-on

Automated tests across four dimensions. They stub the Agora SDK (amd64-only) and MQTT,
so the whole suite runs on any machine with plain Python — no robot, no network.

```bash
pip install -r tests/requirements-test.txt
python -m pytest tests/ -v      # from the add-on directory
```

- **Functional** (`test_functional.py`) — every MQTT command routes to the correct opcode +
  payload; joystick/buttons set the drive vector; telemetry/settings/info map to the right
  Home Assistant state fields; value maps and activity labels.
- **Security** (`test_security.py`) — request-signing regression (verified against the app);
  hostile/malformed payloads never crash and never emit a malformed command; the raw `cmd`
  channel rejects non-integer opcodes; no user credentials or secret files ship in the image;
  the password field is masked.
- **Technical** (`test_technical.py`) — everything compiles; **every** MQTT-discovery config is
  well-formed (unique ids, command topics actually subscribed, selects have options, numbers
  have bounds); `config.yaml`/`build.yaml` valid and the version matches `VERSION.txt`.
- **E2E** (`test_e2e.py`) — full pipeline: MQTT command → real `send()` → exact JSON on the RTM
  wire, and an RTM telemetry message → parsed → HA state. Plus an **opt-in live smoke** that
  logs into the real Enabot cloud and reads the robot list + firmware versions (READ-ONLY,
  never sends a command to the robot):

  ```bash
  EBO_LIVE_TEST=1 EBO_EMAIL=you@example.com EBO_PASSWORD=... python -m pytest tests/test_e2e.py -k live
  ```
