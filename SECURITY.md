# Security

BestYet has **no login**. Anyone who can reach it can read and change the results in the connected Grist document.

- **Do not expose it to the internet.** Run it on a private network, behind your own access control (VPN, private network, or an authenticating proxy).
- The Grist API key is read from the environment and is never sent to the browser or logged. Keep it in a gitignored `.env`, not in the repo or image.
- Use a Grist API key for a user with access only to the one document.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository. Do not open a public issue.
