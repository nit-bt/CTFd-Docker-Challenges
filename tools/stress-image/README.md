# Crash-test image

Every instance holds ~230 MB of RAM and burns its whole CPU share: the worst possible player.
For finding where a challenge server breaks and checking the alerts fire. **Never use in a real event.**

Build on the challenge server:
```bash
cd /opt/ctf/CTFd-Docker-Challenges/tools/stress-image && sudo docker build -t ctf/stress .
```
Steps: ctf-infra `docs/06-testing.md` → "Crash test".
