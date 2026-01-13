# Troubleshooting

## `Error: All connection attempts failed` / CDP problems

- Ensure Chrome is running with a reachable CDP endpoint (the wrappers do this via `bin/ensure_cdp_chrome.sh`).
- If you use `persistent` mode, check `CDP_HOST`/`CDP_PORT` and that `http://127.0.0.1:9222/json/version` responds.

## Headless / DISPLAY

If there is no `$DISPLAY`, set:

```bash
export BROWSER_USE_ALLOW_HEADLESS_FALLBACK=true
```

## `ui-describe` says "LLM not configured"

`ui-describe` needs a vision-capable OpenAI-compatible endpoint:

- `OPENAI_API_BASE` / `OPENAI_BASE_URL`
- `OPENAI_API_KEY`
- optionally `UI_VISION_MODEL`

Without these it will still take the screenshot (and remove overlays), but returns a note instead of a vision description.

