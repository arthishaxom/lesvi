# Cloudflare setup for lesvi (one hostname, one Access policy)

Manual click-through — lesvi does not touch Cloudflare. Assumes:

- `cloudflared` is already running as a system service with a **remotely-managed (token) tunnel** (confirmed on this machine: cloudflared 2026.8.2, `tunnel run --token-file /etc/cloudflared/token`).
- You own a domain on Cloudflare.
- lesvi is running locally on `http://127.0.0.1:8787` (`lesvi serve`).

Do **not** use path-based routes for this. Dashboard path routing does not strip prefixes and its matching semantics are undocumented; one hostname → one port is the supported shape.

## 1. Add the tunnel route

1. Go to **Cloudflare Zero Trust** → **Networks** → **Tunnels** → select the existing tunnel → **Public Hostnames** → **Add a public hostname**.
2. Fill in:
   - **Subdomain**: `lesvi` (or whatever you like; `lesvi.yourdomain.com`)
   - **Domain**: your domain
   - **Path**: leave empty
   - **Service**: `HTTP` → `localhost:8787`
3. Save. Cloudflare creates the DNS record (`CNAME` → `<tunnel-uuid>.cfargotunnel.com`).

Test: `curl -I https://lesvi.yourdomain.com/healthz` should reach the server (it will be public until step 2 is done — ideally do both in one sitting).

## 2. Put Access in front

1. **Zero Trust** → **Access** → **Applications** → **Add an application** → **Self-hosted**.
2. Application:
   - **Name**: `lesvi`
   - **Session duration**: e.g. `1 month`
   - **Public hostname**: `lesvi.yourdomain.com`, path empty.
3. Policy:
   - **Name**: `me`
   - **Action**: `Allow`
   - **Include** → **Emails** → your email address.
4. Save. First visit from any device prompts email OTP (or your configured IdP).

Free tier: Access is free for teams under 50 users; one human = one seat. Service tokens (for scripts) consume no seats.

## 3. Optional hardening

- In the tunnel's public-hostname settings, enable **Protect with Access** so `cloudflared` validates the Access JWT before the request reaches lesvi.
- Add lesvi's own `auth_token` in `~/.config/lesvi/config.toml` as defense in depth; `LESVI_TOKEN` (env, wins) and `--token` also work. Direct local browsing stays exempt; tunneled traffic logs in once at `/login`, and the same token works as `Authorization: Bearer` for scripts. `serve` refuses a non-loopback bind with no token unless you pass `--insecure`.
- Restrict the Access policy further with **Require** → **Country** or device posture if you want.

## 4. Phone

1. Open `https://lesvi.yourdomain.com` on the phone → complete the Access login → lesvi home.
2. Install as PWA ("Add to Home Screen") for a standalone app window.
3. `lesvi url [SHELF [ARTIFACT]]` prints the public URL to open — the home
   page, a shelf, or one artifact by `NNNN` number, slug substring, or exact
   relative path:

   ```sh
   lesvi url                       # home
   lesvi url data-engg             # shelf page
   lesvi url data-engg 25          # lessons/0025-… on that shelf
   ```

## 5. Verify (phone)

The click-through above is accurate when these hold on a real phone:

1. `lesvi status` shows the `public:` URL, and the service is `active`.
2. Opening the public URL prompts for Access OTP once, then lands on the lesvi
   home feed.
3. "Add to Home Screen" installs it; reopening from the icon has no browser
   chrome (standalone display).
4. Opening a lesson, then turning on airplane mode and reopening that lesson
   from history still shows the lesson text from cache (its CSS/JS need the
   network, so it may render unstyled).
5. `lesvi url data-engg <NNNN>` prints a deep link; opening it lands on that
   lesson, not just the home page.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Cloudflare **1016** / origin DNS error | Tunnel stopped or the public hostname was removed |
| **502** from Cloudflare | Route points at the wrong port; check `localhost:8787` matches `lesvi serve` |
| Access login loops forever | Browser blocking cookies for the domain, or the Access app's domain doesn't match the hostname |
| Requests hit lesvi but get the login page | lesvi's fallback token is set and you're not logged in; log in once via `/login` |
| Works locally, 404 on tunnel | Routing path set to something non-empty; clear the Path field |
