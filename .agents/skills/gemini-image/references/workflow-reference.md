# Gemini Image Generation Workflow Reference

| Phase | CLI Command | Purpose |
|---|---|---|
| Setup | `camofox open` + `camofox session load` | Open Gemini with saved session |
| Auth Check | `camofox snapshot` | Verify login status |
| Generate | `camofox type` + `camofox press Enter` | Submit image prompt |
| Wait | `camofox wait 'selector'` | Wait for completion |
| Download | `camofox click` + `camofox downloads --format json` + `curl -o file.png ...` | Download generated image |
| Save | `camofox session save` | Persist session for reuse |
