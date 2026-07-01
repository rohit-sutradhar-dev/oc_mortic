# Mortic OpenCode Sidepod

Native OpenCode TUI sidebar proof for Mortic voice control.

Install locally:

```bash
opencode plugin "file:/absolute/path/to/opencode_mercury_sidepod" --global --force
```

Then start OpenCode. The right sidebar should show a boxed `Mortic` control panel with a pixelated pulsating sphere.

This is only a native UI proof. It does not capture mic audio or call Deepgram yet.

Current interactions:

- `Push to Talk`
- `Live`
- `Clear`
- `Transcript` popup with `C Copy`
- `Handoff` popup
- pulsating sphere while armed/live
