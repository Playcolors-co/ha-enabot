# EBO Air 2 — full command catalog (cloud / Agora RTM)

The robot is driven over **Agora RTM** with JSON messages `{"id": <opcode>, "data": {...}}`.
This add-on wires the common ones to Home Assistant entities; for everything else there is
a **raw command channel** so an automation or an AI agent can send any command.

## Raw command channel (for AI / automations)

Publish to the MQTT topic `ebo_air2/cmd`:

```yaml
service: mqtt.publish
data:
  topic: ebo_air2/cmd
  payload: '{"id": 103501, "data": {"userId": "<yourUserId>", "text": "hello"}}'
```

`id` is the opcode from the table below; `data` is the parameter object (may be omitted for
GET-style opcodes). The bridge adds the session id / timestamp automatically.

> ⚠️ Opcodes marked **(moves)** make the robot drive around. Use them only when you can see
> the robot. This catalog was reverse-engineered from the app's own command builder; simple
> payloads are well understood, the complex ones (marked *object*) may need the exact field
> names from the app's data classes.

## Wired to entities

| entity | opcode | payload |
|--------|--------|---------|
| speed (number) | 103009 | `{"moveSpeed": 1..100}` |
| laser (switch) | 103051 | `{"laser": bool}` |
| move buttons / vector | 101007 | `{"lx","ly","rx","ry","buttons"}` **(moves)** |
| sleep (switch) | 101047 | `{"isSleeping": bool}` |
| say (text) | 103501 | `{"userId","text"}` — robot speaks |
| volume (number) | 102023 | `{"playbackVolume": 0..100, "isPlaybackMuted": bool}` |
| return to base (button) | 103043 | `{"startUp": true}` **(moves)** — no-op if already charging |
| patrol route (select) + start patrol (button) | 104001 → 104002 (list) / 103061 (start) | see below |

**Patrol** — the route select is filled from the robot: request `104001`, the robot replies
`104002` with `{"list":[{"id","routeName","routeFile"}]}`. Start with:
`{"id":103061,"data":{"mode":0,"trackTarget":7,"routeId":-1,"voiceId":""}}` **(moves)** for a
free patrol (no route), or `mode:1` + a real `routeId` to follow a saved route. Routes are
created in the EBO HOME app. No dedicated stop — send any movement to interrupt.

### Not wired as entities (use the raw channel with the right payload)

- **AI tracking** — `{"id":103049,"data":{"mode":<m>,"trackTarget":<t>}}` **(moves)**. In the
  app this is interactive — you tap the subject in the live video to get `mode`/`trackTarget`.

## Full opcode catalog (send via `ebo_air2/cmd`)

**Movement / motion**
- `101007` move vector — `lx,ly,rx,ry,buttons` **(moves)**
- `103001` turn to angle — `{"angle": int}` **(moves)**
- `103005` play preset motion — `{"cycleMode": int, "moveId": int}` **(moves)**
- `103003` choreography — `{"cycleMode": int, "moveIds": [], "voiceIds": [], "emojiIds": []}` **(moves)**
- `103011` move mode — `{"moveMode": int}`
- `103009` speed — `{"moveSpeed": int}`
- `103043` **return to base now** — `{"startUp": true}` **(moves)** — no-op if already charging
- `103019` auto-recharge *settings* (not a "go home" command) —
  `{"status","lowBattery","lowBatteryPercentage","timedEnable","startHour","startMinute","dndNight","chargeType"}`
- `103023` motion settings — *motionSettings object*
- `103049` start AI tracking — `{"mode": int, "trackTarget": int}` **(moves)** — interactive (pick subject)
- `103061` start patrol — `{"mode": int, "routeId": int, "trackTarget": int, "voiceId": str}` **(moves)** — needs a saved route
- `103401` object tracking — `{"enable": int, "objectId": int}` **(moves)**

**Audio / voice / conversational AI**
- `103007` play voice — `{"cycleMode": int, "voiceId": int}`
- `103501` text-to-speech — `{"userId": str, "text": str}`
- `102023` playback volume — `{"playbackVolume": int, "isPlaybackMuted": bool}`
- `102031` talkback volume — `{"talkbackVolume": int}`
- `103027` voice call — *voiceCall object*
- `103301` / `103305` ask the AI — `{"session","question"/"questionId","userId","modelType"}`
- `103343` Agora AI-agent config — *agoraAiAgentConfig object*

**Camera / media**
- `102035` shoot mode (photo/video) — `{"shootMode": int}`
- `102055` video quality — `{"videoQuality": int}`
- `102057` image style — `{"imageStyle": int}`
- `104013` scheduled snapshot — *snapshotTask object*
- `104099` upload video to cloud — `{"videoUploadCloud": bool}`
- `101049` sport/motion recording — `{"sportsRecord": bool}`

**Eyes / emoji** (the Air 2 has an eye display)
- `104057` eyes emoji mode — *eyesEmojiMode object*

**State / system**
- `101003` handshake — `{"userId": str}`
- `101005` heartbeat — `{"state": int}`
- `101047` sleep/wake — `{"isSleeping": bool}`
- `101013` set timezone — `{"timezone": long}`
- `101017` set region · `101021` set language
- `101029` firmware update — *upgradeFirmware object*
- `101061` roam mode — `{"isRoamOn": bool, "sensitivity": int}`
- `101065` auto-switch — `{"autoSwitch": bool, "sensitivity": int}`
- `103041` do-not-disturb — *dnd object* · `103047` `{"safeMode": int}` (`103043` = return-to-base, see Movement)
- `103093` privacy — *privacy object* · `103083` sound/picture — *soundPicture object*

**GET (query state, no payload):** 101025, 101027 (settings), 101041, 101059, 101063,
101067, 101081, 103017/21/25/39/55/63/81/91/101/201/307/309, 104001/11/15/21/25/31/35/39/55/61/97.
State comes back as report `101026` (telemetry) and `101004`/`101006`.

Complex payload field names live in the app classes `com/enabot/lib_ebo/robot/air2/rtm/*`
and `com/enabot/lib_device/x/msg/*` (decompile the APK to inspect).
