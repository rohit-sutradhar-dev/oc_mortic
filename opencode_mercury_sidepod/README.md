# Mortic OpenCode Sidepod

Native OpenCode TUI sidebar proof for Mortic voice control.

Tested against OpenCode 1.17.13. The plugin uses `api.keymap.registerLayer` for commands; `api.command` is deprecated for OpenCode v2 and is not used. The package ships `src/` directly — there is no build step.

Run the package fixture tests:

```bash
npm test
```

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
