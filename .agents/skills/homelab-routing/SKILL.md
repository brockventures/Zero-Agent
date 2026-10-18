---
name: homelab-routing
description: Standard operating procedure for provisioning, configuring, exposing, and routing homelab service URLs across Cloudflare Tunnel, Nginx wildcard SSL reverse proxy, and UniFi static DNS shortnames.
---

# Homelab Service URL & Port-Free Shortname Provisioning (The 3-Layer Standard)

Whenever asked to configure, expose, route, or create a URL for any homelab service (e.g., `service.brock.ventures`), Zero MUST provision the complete, port-free 3-layer stack in a single pass:

---

## 1. The 3-Layer Deployment Standard

### Layer 1: External Cloudflare Tunnel & Public DNS
- **Ingress Rule:** Inject an ingress rule in the `zero-health` Cloudflare tunnel (`2befb2df-ea17-4ae6-b13b-38011fa1a567`) pointing `service.brock.ventures` to the local backend service (e.g. `http://<HOST_2_IP>:<port>`).
- **Public DNS Record:** Create a proxied `CNAME` record for `service.brock.ventures` targeting `2befb2df-ea17-4ae6-b13b-38011fa1a567.cfargotunnel.com` via `tools/cloudflare.py`.

### Layer 2: Host 1 Nginx Reverse Proxy & Wildcard SSL
- **SSL Termination:** Add a server block in `/etc/nginx/conf.d/http.brock_ventures_ssl.conf` on Host 1 (`<HOST_1_IP>`) terminating the Let's Encrypt wildcard certificate (`$NAS_DOCKER_ROOT/scripts/.acme.sh/brock.ventures_ecc/fullchain.cer`).
- **HTTP-to-HTTPS & WebSockets:** Include port 80 301-redirect to `https://$host$request_uri` and proxy WebSocket headers (`Upgrade $http_upgrade`, `Connection $connection_upgrade`) with 600s timeouts.
- **Verification:** Always run `sudo nginx -t && sudo nginx -s reload` on Host 1 to verify syntax before declaring success.

### Layer 3: UniFi Static DNS & Single-Word Shortname (`service/`)
- **Controller Static DNS API:** Post static A-records via UniFi Network API (`/proxy/network/v2/api/site/default/static-dns`) using the `zero` service account and CSRF token:
  1. `service.brock.ventures` -> `<HOST_1_IP>`
  2. `service.home` -> `<HOST_1_IP>`
- **Naked Shortname Catch-All:** On UCG-Max (`<GATEWAY_IP>`), ensure `address=/service/<HOST_1_IP>` is appended to `/data/udapi-config/brock_ventures.conf` and symlinked to `/run/dnsmasq.dhcp.conf.d/` so single-word browser shortcuts (`service/`) resolve cleanly on the local LAN. Host 1's `/etc/nginx/conf.d/http.shortname_redirect.conf` automatically 302-redirects naked shortnames to `https://$shortname.brock.ventures/`.

---

## 2. Verification Checklist Before Reporting Done
1. Public HTTPS test: `curl -IL https://service.brock.ventures` returns HTTP 200.
2. Local Shortname test: `curl -IL http://service/` (or `Host: service` to `<HOST_1_IP>`) returns HTTP 302 redirecting to `https://service.brock.ventures/`.
3. Green Padlock: Ensure zero certificate mismatch or mixed-content warnings.
