# Popup images (optional)

Put an image here named after the popup it belongs to, and players see it above the popup text.
No file = plain popup.

| File name (any of .png .jpg .jpeg .webp .gif) | Shown when |
|---|---|
| `started` | instance started |
| `stopped` | player pressed Stop |
| `cooldown` | "Please wait N seconds…" after Stop |
| `one-at-a-time` | "Another container is already running…" |
| `revert-wait` | "Revert unlocks …" (pressed Revert too early) |
| `busy` | "Your previous request is still running…" (double click) |
| `error` | any other error |
| `solved`, `solved-1` ... `solved-20` | correct flag ("Flag captured!" popup from the CTFC theme). Add several and one is picked at random each time. |

Example: `cooldown.png`. Keep each image small (under ~300 KB), every popup loads it.
After adding/replacing: commit, `git pull` + `cp -r docker_challenges ...` on the platform, restart ctfd.
