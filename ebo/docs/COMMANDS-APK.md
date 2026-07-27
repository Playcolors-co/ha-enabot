# EBO Air 2 — Complete command catalog (from the APK)

**Transport:** Agora RTM `publish`. **Message:** `{"id":<opcode>,"sid":"<sessione>","data":{...},"type":0,"timestamp":<ms>}`.
**Heartbeat:** `101005 {state:0}` every ~2s (required to keep the session alive).
**Joystick (101007):** `{lx,ly,rx,ry,buttons}`, values ~ -100..100. `ly<0`=forward, `ly>0`=backward, `rx<0`=turn left, `rx>0`=turn right, `buttons:1`=active. Pair (value, then 0=stop).

**Total: 112 commands. Already in the add-on: 20. To add: 92.**

| Opcode | Category | Name (inferred) | Parameters | In add-on |
|---|---|---|---|---|
| 101003 | System/Session |  | userId | ✅ |
| 101005 | System/Session | heartbeat (state, every 2s) | state | ✅ |
| 101007 | System/Session | analog JOYSTICK (movement) | lx, ly, rx, ry, buttons | ✅ |
| 101009 | System/Session | set by type | type | ⬜ |
| 101013 | System/Session | time sync (timestamp) | — | ⬜ |
| 101017 | System/Session | set region | region | ⬜ |
| 101021 | System/Session |  | — | ⬜ |
| 101023 | System/Session |  | — | ⬜ |
| 101025 | System/Session |  | — | ⬜ |
| 101027 | System/Session |  | — | ✅ |
| 101029 | System/Session |  | — | ⬜ |
| 101033 | System/Session | upload log to cloud | userId, robotId, uploadLogToken, desc, contact | ⬜ |
| 101039 | System/Session | set by type | type | ⬜ |
| 101041 | System/Session |  | — | ⬜ |
| 101047 | System/Session | sleep / wake | isSleeping | ✅ |
| 101049 | System/Session | "sports" recording | sportsRecord | ✅ |
| 101059 | System/Session | action | — | ⬜ |
| 101061 | System/Session | auto roaming (on, sensitivity) | isRoamOn, sensitivity | ⬜ |
| 101063 | System/Session | action | — | ⬜ |
| 101065 | System/Session | auto-switch (sensitivity) | autoSwitch, sensitivity | ⬜ |
| 101067 | System/Session | action | — | ⬜ |
| 101081 | System/Session | action | — | ⬜ |
| 101901 | System/Session |  | — | ⬜ |
| 101903 | System/Session |  | — | ⬜ |
| 101905 | System/Session |  | — | ⬜ |
| 101907 | System/Session |  | — | ⬜ |
| 102001 | Audio/Camera | set audio/media type | type | ⬜ |
| 102003 | Audio/Camera | set type | type | ⬜ |
| 102005 | Audio/Camera | set type | type | ⬜ |
| 102007 | Audio/Camera | set type | type | ⬜ |
| 102011 | Audio/Camera |  | — | ⬜ |
| 102013 | Audio/Camera |  | — | ⬜ |
| 102015 | Audio/Camera |  | — | ⬜ |
| 102017 | Audio/Camera |  | — | ⬜ |
| 102023 | Audio/Camera | playback volume (+mute) | playbackVolume, isPlaybackMuted | ✅ |
| 102031 | Audio/Camera | talkback volume (mic) | talkbackVolume | ✅ |
| 102035 | Audio/Camera | shootMode (photo/video) | shootMode | ✅ |
| 102037 | Audio/Camera |  | — | ⬜ |
| 102039 | Audio/Camera |  | — | ⬜ |
| 102055 | Audio/Camera | video QUALITY | videoQuality | ⬜ |
| 102057 | Audio/Camera | image style (filter) | imageStyle | ⬜ |
| 102101 | Audio/Camera |  | — | ⬜ |
| 103001 | Movement/AI/Skill | ROTATION by angle | angle | ⬜ |
| 103003 | Movement/AI/Skill | create routine (moves+voices+emoji) | cycleMode, moveIds, voiceIds, emojiIds | ⬜ |
| 103005 | Movement/AI/Skill | run move | cycleMode, moveId | ✅ |
| 103007 | Movement/AI/Skill | run voice | cycleMode, voiceId | ✅ |
| 103009 | Movement/AI/Skill | movement SPEED | moveSpeed | ✅ |
| 103011 | Movement/AI/Skill | movement mode | moveMode | ✅ |
| 103013 | Movement/AI/Skill |  | — | ⬜ |
| 103015 | Movement/AI/Skill |  | — | ⬜ |
| 103017 | Movement/AI/Skill |  | — | ⬜ |
| 103019 | Movement/AI/Skill | auto-recharge setting | — | ⬜ |
| 103021 | Movement/AI/Skill |  | — | ⬜ |
| 103023 | Movement/AI/Skill |  | — | ⬜ |
| 103025 | Movement/AI/Skill |  | — | ⬜ |
| 103027 | Movement/AI/Skill |  | — | ⬜ |
| 103029 | Movement/AI/Skill |  | sn | ⬜ |
| 103039 | Movement/AI/Skill |  | — | ⬜ |
| 103041 | Movement/AI/Skill |  | — | ⬜ |
| 103043 | Movement/AI/Skill | RETURN to base (dock) | startUp | ✅ |
| 103047 | Movement/AI/Skill | safe mode | safeMode | ⬜ |
| 103049 | Movement/AI/Skill | start AI-track | — | ✅ |
| 103055 | Movement/AI/Skill |  | — | ⬜ |
| 103061 | Movement/AI/Skill | start patrol | — | ✅ |
| 103063 | Movement/AI/Skill |  | — | ⬜ |
| 103071 | Movement/AI/Skill | auto-rec during call | callAutoRecording | ✅ |
| 103081 | Movement/AI/Skill | action | — | ⬜ |
| 103083 | Movement/AI/Skill |  | — | ⬜ |
| 103091 | Movement/AI/Skill |  | — | ⬜ |
| 103093 | Movement/AI/Skill |  | — | ⬜ |
| 103095 | Movement/AI/Skill |  | — | ⬜ |
| 103101 | Movement/AI/Skill |  | — | ⬜ |
| 103103 | Movement/AI/Skill |  | — | ⬜ |
| 103201 | Movement/AI/Skill |  | — | ⬜ |
| 103301 | Movement/AI/Skill | AI conversation (ask) | modelType, session, question, userId | ⬜ |
| 103305 | Movement/AI/Skill | AI conversation (session) | session, questionId, userId | ⬜ |
| 103307 | Movement/AI/Skill |  | — | ⬜ |
| 103309 | Movement/AI/Skill |  | — | ⬜ |
| 103341 | Movement/AI/Skill |  | — | ⬜ |
| 103343 | Movement/AI/Skill |  | — | ⬜ |
| 103345 | Movement/AI/Skill |  | — | ⬜ |
| 103401 | Movement/AI/Skill | AI object-track (on, objectId) | enable, objectId | ⬜ |
| 103501 | Movement/AI/Skill | TTS - say text | userId, text | ✅ |
| 104001 | File/Recordings/Eyes |  | — | ✅ |
| 104003 | File/Recordings/Eyes |  | — | ⬜ |
| 104005 | File/Recordings/Eyes | delete file (ids) | ids | ⬜ |
| 104011 | File/Recordings/Eyes |  | — | ⬜ |
| 104013 | File/Recordings/Eyes | snapshot | — | ⬜ |
| 104015 | File/Recordings/Eyes |  | — | ⬜ |
| 104017 | File/Recordings/Eyes |  | — | ⬜ |
| 104019 | File/Recordings/Eyes | delete (ids) | ids | ⬜ |
| 104021 | File/Recordings/Eyes |  | — | ⬜ |
| 104023 | File/Recordings/Eyes | scheduled recording | — | ⬜ |
| 104025 | File/Recordings/Eyes |  | — | ⬜ |
| 104027 | File/Recordings/Eyes |  | — | ⬜ |
| 104029 | File/Recordings/Eyes | delete (ids) | ids | ⬜ |
| 104031 | File/Recordings/Eyes |  | — | ⬜ |
| 104033 | File/Recordings/Eyes |  | — | ⬜ |
| 104035 | File/Recordings/Eyes |  | — | ⬜ |
| 104037 | File/Recordings/Eyes |  | — | ⬜ |
| 104039 | File/Recordings/Eyes |  | — | ⬜ |
| 104055 | File/Recordings/Eyes |  | — | ⬜ |
| 104057 | File/Recordings/Eyes | eyes/emoji mode | — | ⬜ |
| 104061 | File/Recordings/Eyes |  | — | ⬜ |
| 104093 | File/Recordings/Eyes | video encryption (secretKey) | deviceEncryption, secretKey | ⬜ |
| 104095 | File/Recordings/Eyes | set secretKey | secretKey | ⬜ |
| 104097 | File/Recordings/Eyes |  | — | ⬜ |
| 104099 | File/Recordings/Eyes | upload video to cloud | videoUploadCloud | ✅ |
| 104401 | File/Recordings/Eyes |  | — | ⬜ |
| 106003 | Misc |  | — | ⬜ |
| 106005 | Misc |  | — | ⬜ |
| 198001 | Meta | generic command (commandId) | commandId | ⬜ |
