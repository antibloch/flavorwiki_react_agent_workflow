# Herbalife Agent Deployment on Hetzner

This is the production runbook for deploying the `herbalife_agent` FastAPI/LangGraph service from
GitLab CI/CD to one Ubuntu server on Hetzner. It is called by another backend and streams NDJSON
responses from `POST /ask`.

The examples assume:

- Ubuntu Server 24.04 LTS.
- The checked-in `herbalife_agent/Dockerfile`, `compose.production.yaml`, and `ops/deploy.sh`.
- The application listens on container port 8000 and exposes GET /health.
- GitLab Container Registry stores immutable images tagged with the commit SHA.
- Host Nginx terminates TLS and proxies to 127.0.0.1:8000.
- The production branch is the GitLab default branch.
- The deployment directory is /opt/herbalife-agent.
- The initial server login is root, but CI deploys as the deploy user.

The repository currently has a GitHub origin. Create a GitLab project by importing or mirroring it,
enable GitLab Container Registry, and keep GitLab synchronized before using this pipeline.

### How to read the command examples

Text after `#` is a short explanation; Bash ignores it, so you can paste the complete line. A
comment immediately above a command explains a multi-line command. In a `tee ... <<'EOF'` block,
the lines between the two `EOF` markers are written into a configuration file; the closing `EOF`
ends the input and must remain on a line by itself.

## Architecture

| Component | Responsibility | Internet exposure |
|---|---|---|
| GitLab runner | Test, build, and push an immutable image | None on the server |
| GitLab Container Registry | Store commit-tagged images | HTTPS |
| deploy user | Pull images and run Docker Compose through SSH | SSH only |
| Docker Compose | Run and health-check the Python agent | Bound to 127.0.0.1 only |
| Nginx | TLS reverse proxy with NDJSON response buffering disabled | TCP 80 and 443 |
| Certbot | Issue and renew the Let's Encrypt certificate | Uses TCP 80 challenge |

Request flow:

~~~text
Client -> HTTPS :443 -> Nginx -> http://127.0.0.1:8000 -> Python container
GitLab CI -> SSH :22 -> deploy user -> Docker Compose -> GitLab registry
~~~

## Values to decide before starting

Replace these examples consistently:

| Name | Example |
|---|---|
| Server IPv4 | 203.0.113.10 |
| DNS name | herbalife-agent.gpisurveys.com |
| SSH port | 22 |
| Deploy user/group | deploy |
| Application directory | /opt/herbalife-agent |
| Compose project name | herbalife-agent |
| Container port | 8000 |
| Health URL | http://127.0.0.1:8000/health |
| Certificate email | YOUR_REAL_EMAIL@gpisurveys.com |

Do not paste real passwords, API keys, model-provider keys, database credentials, or private SSH keys into this document, the repository, Compose YAML, or CI logs.

## 1. Configure Hetzner and DNS

In Hetzner Cloud Console:

1. Attach a Cloud Firewall to the server.
2. Permit inbound TCP 22 only from trusted administrator IP ranges when possible.
3. Permit inbound TCP 80 and 443 from 0.0.0.0/0 and ::/0.
4. Keep outbound traffic allowed so Docker, GitLab, package repositories, model APIs, and Let's Encrypt remain reachable.
5. Enable Hetzner backups or create a snapshot before material upgrades.

Create DNS records before requesting TLS:

~~~text
A     herbalife-agent.gpisurveys.com     203.0.113.10
AAAA  herbalife-agent.gpisurveys.com     SERVER_IPV6_ADDRESS
~~~

Only create the AAAA record if IPv6 is configured and inbound 80/443 work over IPv6. A broken AAAA record commonly causes certificate validation failures.

Verify DNS from an administrator machine:

~~~bash
dig +short A herbalife-agent.gpisurveys.com       # Show the domain's IPv4 address.
dig +short AAAA herbalife-agent.gpisurveys.com    # Show the domain's IPv6 address, if configured.
~~~

Hetzner Cloud Firewalls deny inbound traffic that is not explicitly allowed. See the [Hetzner Cloud Firewall documentation](https://docs.hetzner.com/cloud/firewalls/overview/).

## 2. Initial root login and operating-system bootstrap

Log in from a trusted machine:

~~~bash
ssh root@203.0.113.10    # Open a root shell on the new server over SSH.
~~~

Confirm the distribution before running Ubuntu-specific commands:

~~~bash
cat /etc/os-release          # Confirm the installed Linux distribution and version.
uname -a                     # Show the running kernel and machine information.
dpkg --print-architecture    # Confirm the Debian/Ubuntu package architecture.
~~~

Update the server and install baseline tools:

~~~bash
apt-get update    # Refresh the available package list.
DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade    # Install all OS upgrades without prompts.
apt-get install -y ca-certificates curl dnsutils gnupg jq git nano openssh-server nginx snapd sudo ufw fail2ban unattended-upgrades    # Install deployment, web, firewall, and security tools.
timedatectl set-timezone UTC    # Use UTC for consistent server and log timestamps.
hostnamectl set-hostname herbalife-agent-prod    # Assign a recognizable hostname to the server.
systemctl enable --now ssh nginx fail2ban    # Start these services now and after every reboot.
~~~

If the kernel or libc was upgraded, reboot before continuing:

~~~bash
test -f /var/run/reboot-required && cat /var/run/reboot-required    # Show why Ubuntu requests a reboot, if it does.
reboot    # Restart the server so kernel and core-library updates take effect.
~~~

Reconnect after the reboot.

Enable automatic security updates:

~~~bash
dpkg-reconfigure -plow unattended-upgrades    # Enable and configure automatic security updates.
systemctl status unattended-upgrades --no-pager    # Confirm the update service is available.
~~~

## 3. Host firewall

Keep the current SSH session open while configuring UFW. Confirm the SSH rule before enabling the firewall:

~~~bash
ufw default deny incoming    # Block unsolicited inbound traffic by default.
ufw default allow outgoing   # Allow the server to reach package, GitLab, and API services.
ufw allow 22/tcp comment 'SSH'    # Keep the standard SSH port reachable.
ufw allow 80/tcp comment 'HTTP for Nginx and ACME'    # Allow HTTP and Let's Encrypt validation.
ufw allow 443/tcp comment 'HTTPS'    # Allow encrypted application traffic.
ufw enable    # Activate the firewall; confirm when prompted.
ufw status verbose    # Display the active rules and default policies.
~~~

If SSH uses a non-default port, allow that port before enabling UFW and use the same port in every later command.

Docker-published ports can bypass some UFW rules. This runbook therefore publishes the application only on 127.0.0.1. Do not change the Compose mapping to 0.0.0.0:8000:8000.

Enable the Fail2ban SSH jail. Replace port = ssh if SSH uses a custom port:

~~~bash
# Write the SSH jail settings; the lines until EOF become the file contents.
tee /etc/fail2ban/jail.d/sshd.local >/dev/null <<'EOF'
[sshd]
enabled = true
port = ssh
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl restart fail2ban    # Reload Fail2ban with the new SSH jail.
fail2ban-client status sshd   # Confirm the SSH jail is active and show its bans.
~~~

The jail file means:

- `[sshd]` selects Fail2ban's built-in SSH filter.
- `enabled = true` turns the jail on.
- `port = ssh` watches the standard SSH service, which is TCP 22.
- `maxretry = 5` allows five failed attempts within `findtime = 10m`.
- `bantime = 1h` blocks the offending address for one hour.

## 4. Create the deploy user and group

Ubuntu's adduser creates both a user named deploy and a matching primary group:

~~~bash
adduser --disabled-password --gecos '' deploy    # Create the deploy user and its matching group.
passwd deploy    # Set a console-recovery password for the deploy user.
id deploy    # Show the user's UID and group memberships.
getent group deploy    # Confirm that the deploy group exists.
~~~

The password belongs to the user, not the group. Linux group passwords are discouraged and are not required. The deploy password is useful for Hetzner console recovery, but SSH password authentication will be disabled after key login is verified.

Create the production directories:

~~~bash
install -d -m 0750 -o deploy -g deploy /opt/herbalife-agent    # Create the private app directory owned by deploy.
~~~

Create application-specific persistent directories only when required. Their numeric owner must match the non-root UID inside the container.

Do not grant deploy unrestricted sudo merely for CI. Docker group membership already grants root-equivalent control of the host, so protect this SSH key like a root credential.

## 5. Create and install the dedicated GitLab deployment SSH key

Generate a dedicated key on a trusted administrator machine, not inside a CI job and not as the root server key:

~~~bash
ssh-keygen -t ed25519 -a 100 -C 'gitlab-production-deploy' -f ~/.ssh/herbalife_agent_gitlab_deploy -N ''    # Create a dedicated passwordless CI deployment key pair.
~~~

This creates:

- ~/.ssh/herbalife_agent_gitlab_deploy — private key; store only in GitLab.
- ~/.ssh/herbalife_agent_gitlab_deploy.pub — public key; install on the server.

Display and copy the public key:

~~~bash
cat ~/.ssh/herbalife_agent_gitlab_deploy.pub    # Print only the public key that is safe to copy to the server.
~~~

As root on the server, install that one public key. Replace the placeholder with the complete public key:

~~~bash
install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh    # Create deploy's SSH directory with private permissions.
touch /home/deploy/.ssh/authorized_keys    # Create the authorized-key list if it does not exist.
chmod 0600 /home/deploy/.ssh/authorized_keys    # Allow only deploy to read or edit the key list.
chown deploy:deploy /home/deploy/.ssh/authorized_keys    # Ensure deploy owns the key list.
nano /home/deploy/.ssh/authorized_keys    # Paste the complete public-key line, save, and exit.
~~~

Verify ownership:

~~~bash
namei -l /home/deploy/.ssh/authorized_keys    # Show owner and permissions for every directory in the path.
~~~

From a second administrator terminal, test the key before changing SSH configuration:

~~~bash
ssh -i ~/.ssh/herbalife_agent_gitlab_deploy deploy@203.0.113.10    # Test login using the new private key.
whoami    # Confirm that the remote session is running as deploy.
exit      # Close the test SSH session.
~~~

### Optional restriction for the CI key

For stronger isolation, prefix the public-key line in authorized_keys with restrictions:

~~~text
no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 AAAA... gitlab-production-deploy
~~~

Do not use a forced command until deployment works, because a forced-command wrapper must explicitly permit scp or sftp and the deployment commands.

## 6. Harden SSH after deploy login works

**Execution order:** prepare and review this section now, but do not reload the hardened SSH configuration until the root-only Docker, Nginx, and Certbot work in sections 7 through 11 is complete. Keep the initial root session open throughout. If you already have a separate tested sudo administrator, it is safe to apply this section immediately.

Keep the existing root session open. Create an override:

~~~bash
# Write a separate SSH hardening override without modifying Ubuntu's main file.
tee /etc/ssh/sshd_config.d/99-production-hardening.conf >/dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
AllowTcpForwarding no
MaxAuthTries 3
LoginGraceTime 30
AllowUsers deploy
EOF
~~~

The SSH override disables root, password, keyboard-interactive, X11, and TCP-forwarding access. It
keeps public-key authentication enabled, allows three authentication failures, permits 30 seconds
to log in, and limits SSH access to the `deploy` account. Add an administrator account to
`AllowUsers` before reloading if one must retain SSH access.

Validate before reloading:

~~~bash
sshd -t                 # Check the complete SSH configuration for syntax errors.
systemctl reload ssh    # Apply valid settings without terminating current sessions.
~~~

Open a new terminal and test deploy login again. Do not close the original root session until the new login succeeds:

~~~bash
ssh -i ~/.ssh/herbalife_agent_gitlab_deploy deploy@203.0.113.10    # Prove key login still works after hardening.
~~~

If administrators need a separate SSH account, create that account and add it to AllowUsers before disabling root login. Do not share the CI deploy key with humans.

## 7. Install Docker Engine and Docker Compose v2

Run these commands from the still-open root session. They use Docker's official Ubuntu repository:

~~~bash
apt-get remove -y docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc || true    # Remove packages that conflict with Docker's official packages.
install -m 0755 -d /etc/apt/keyrings    # Create the directory for trusted repository keys.
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc    # Download Docker's package-signing key.
chmod a+r /etc/apt/keyrings/docker.asc    # Let the package manager read the signing key.
# Add Docker's official repository using this Ubuntu release and CPU architecture.
tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update    # Refresh packages again so the new Docker repository is included.
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin    # Install Docker Engine, Buildx, and Compose v2.
systemctl enable --now docker    # Start Docker now and automatically after reboot.
docker version    # Display client/server versions and confirm the daemon responds.
docker compose version    # Confirm the Compose v2 plugin is installed.
docker run --rm hello-world    # Pull and run a disposable container as an end-to-end test.
~~~

In the Docker repository file, `Types` selects binary packages, `URIs` names Docker's official
repository, `Suites` automatically selects this Ubuntu release, `Components` chooses the stable
channel, `Architectures` uses this machine's CPU architecture, and `Signed-By` requires packages to
match the downloaded Docker signing key.

The supported command is docker compose with a space. The docker-compose standalone binary is legacy and is not installed.

Add deploy to Docker's group:

~~~bash
usermod -aG docker deploy    # Grant deploy permission to control Docker.
id deploy    # Confirm docker appears in deploy's supplementary groups.
~~~

Log out and back in so group membership refreshes, then verify as deploy:

~~~bash
docker version    # Confirm deploy can contact the Docker daemon.
docker compose version    # Confirm Compose is available in deploy's new login session.
docker run --rm hello-world    # Verify deploy can create and remove a container.
~~~

Official references:

- [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Install the Docker Compose plugin](https://docs.docker.com/compose/install/linux/)

### Configure bounded Docker logs

If /etc/docker/daemon.json already exists, merge these settings rather than overwriting it:

~~~bash
test -e /etc/docker/daemon.json && cat /etc/docker/daemon.json    # Inspect existing Docker settings before replacing anything.
~~~

For a new server:

~~~bash
install -d -m 0755 /etc/docker    # Create Docker's configuration directory if needed.
# Write daemon settings that preserve containers and rotate local JSON logs.
tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "live-restore": true,
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "5"
  }
}
EOF
systemctl restart docker    # Restart Docker so the daemon reads the new settings.
docker info                 # Confirm Docker starts and report its effective configuration.
~~~

The Docker JSON keeps running containers alive during a daemon restart, uses the standard JSON log
driver, rotates each log at 20 MB, and retains no more than five log files. JSON does not allow
comments, which is why the explanations are outside the copied block.

## 8. Understand what comes from the repository

The Herbalife repository is **not cloned on the Hetzner server**. The repository contains these
deployment assets, but GitLab CI/CD uses them from the GitLab runner:

- `Dockerfile` runs Python 3.12 and Uvicorn as UID/GID 10001 with exactly one worker.
- `compose.production.yaml` pulls `${APP_IMAGE}`, loads `app.env`, publishes only
  `127.0.0.1:8000`, drops Linux capabilities, bounds logs, and health-checks `/health`.
- `ops/deploy.sh` records the immutable image, waits for container health, and rolls back
  automatically when a different previously healthy image is available.

During a production pipeline:

1. The GitLab runner checks out the repository.
2. It builds `Dockerfile` and pushes the resulting image to GitLab Container Registry.
3. It copies only `compose.production.yaml` and `ops/deploy.sh` to `/opt/herbalife-agent` over SSH.
4. It tells the server to pull the image from the registry and start it with Docker Compose.

The Python source is inside the Docker image. There is no `git clone` or `git pull` on the server.

### Optional local validation

**Run on:** your development computer, from the cloned repository root.
**Do not run on:** the Hetzner server, because `.env.example` and `ops/deploy.sh` are not there
before the first pipeline.

These checks are optional because the GitLab `validate` job performs equivalent validation:

~~~bash
# Create a disposable environment file so Compose can resolve its env_file reference.
cp .env.example /tmp/herbalife-agent-app.env
# Render and validate production Compose without starting containers.
APP_IMAGE=registry.gitlab.com/GROUP/PROJECT:test \
  docker compose --env-file /tmp/herbalife-agent-app.env -f compose.production.yaml config --quiet
bash -n ops/deploy.sh    # Check the deployment script's Bash syntax without executing it.
~~~

If Docker Compose is not installed on your development computer, skip the Compose command and let
the GitLab validation job run it.

The image must remain single-worker. The current API stores conversation checkpoints and survey
scope in process memory; adding workers can split follow-up threads and produce incorrect routing.

### Application secret file on the server

**Run on:** the Hetzner server as `root`.

This is the one application file that you create manually on the server. CI must never construct,
replace, or print it:

~~~bash
install -m 0600 -o deploy -g deploy /dev/null /opt/herbalife-agent/app.env    # Create an empty secret file readable only by deploy.
sudo -u deploy nano /opt/herbalife-agent/app.env    # Edit secrets as deploy so ownership stays correct.
~~~

Copy the key names from the repository `.env.example`. The mandatory values are:

~~~dotenv
OPENAI_API_KEY=replace-me
DATABASE_URL=postgresql://readonly-user:password@database-host:5432/database-name
CHARTS_API_BASE_URL=https://charts-api.gpisurveys.com
CHARTS_STATS_EXTERNAL_ACCESS_SECRET=replace-me
HERBALIFE_EXTERNAL_ACCESS_SECRET=replace-with-output-from-openssl-rand-hex-32
~~~

Use a database role that cannot write. Add optional model, cache, CORS, and timeout settings
from `.env.example`, then enforce ownership:

~~~bash
chown deploy:deploy /opt/herbalife-agent/app.env    # Correct the secret file's owner and group.
chmod 0600 /opt/herbalife-agent/app.env             # Permit only deploy to read or write secrets.
stat /opt/herbalife-agent/app.env                   # Display ownership, permissions, and timestamps.
~~~

Never print rendered environment values from `docker compose config` in CI logs.

## 9. Deployment behavior

You do not need to run the next command during a normal deployment. The GitLab job runs it remotely
after uploading `compose.production.yaml` and `ops/deploy.sh` and authenticating Docker with the
read-only registry token. It is shown here only to explain the final CI step:

~~~bash
# Set the app directory for this command and deploy one exact immutable image.
DEPLOY_PATH=/opt/herbalife-agent /opt/herbalife-agent/deploy.sh \
  registry.gitlab.com/GROUP/PROJECT:COMMIT_SHA
~~~

The script refuses to deploy without readable `app.env`, validates the image and Compose model,
pulls before replacement, waits up to 180 seconds for health, and restores the previous image
on failure. The first deployment has no previous image to restore.

Database migrations are absent because this service only reads the existing survey database.

After the first successful pipeline, the server directory contains:

| Server path | How it arrives |
|---|---|
| `/opt/herbalife-agent/app.env` | Created manually once by root; never uploaded by CI |
| `/opt/herbalife-agent/compose.production.yaml` | Uploaded by GitLab CI over SSH |
| `/opt/herbalife-agent/deploy.sh` | Uploaded by GitLab CI over SSH |
| `/opt/herbalife-agent/.compose.env` | Created by `deploy.sh` after an image becomes healthy |

The application source is not listed because it remains inside the pulled Docker image.

## 10. Install and configure Nginx

Nginx was installed in the baseline step. Create the following configuration at
/etc/nginx/sites-available/herbalife-agent for herbalife-agent.gpisurveys.com:

~~~nginx
server {
    # Accept unencrypted IPv4 and IPv6 traffic; Certbot later redirects it to HTTPS.
    listen 80;
    listen [::]:80;
    # Handle requests only for this DNS name.
    server_name herbalife-agent.gpisurveys.com;

    # Reject unexpectedly large request bodies.
    client_max_body_size 25m;

    location / {
        # Forward every path to the loopback-only FastAPI container.
        proxy_pass http://127.0.0.1:8000;
        # Use HTTP/1.1 for reliable streaming responses.
        proxy_http_version 1.1;

        # Preserve the requested hostname and the original client/protocol information.
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Fail fast when connecting, but allow long-running AI requests to stream.
        proxy_connect_timeout 10s;
        proxy_send_timeout 3600s;
        proxy_read_timeout 3600s;

        # Send NDJSON chunks immediately and never cache an agent response.
        proxy_buffering off;
        proxy_cache off;
        add_header X-Accel-Buffering no;
    }
}
~~~

Write and enable it:

~~~bash
nano /etc/nginx/sites-available/herbalife-agent    # Create the site file using the configuration above.
ln -s /etc/nginx/sites-available/herbalife-agent /etc/nginx/sites-enabled/herbalife-agent    # Enable the site through Nginx's enabled-sites directory.
rm -f /etc/nginx/sites-enabled/default    # Disable Ubuntu's default placeholder website.
nginx -t                                  # Validate all Nginx configuration before applying it.
systemctl reload nginx                    # Apply valid settings without a full service restart.
systemctl status nginx --no-pager         # Confirm Nginx is running and show recent status details.
~~~

Test HTTP:

~~~bash
curl -I http://herbalife-agent.gpisurveys.com/    # Request response headers through DNS and Nginx.
~~~

A 502 response is expected before the first application deployment. At this stage, verify that DNS
reaches this Nginx server; CI checks the backend after starting the container.

This API is NDJSON over ordinary streaming HTTP, not WebSocket or SSE. It currently has no built-in
authentication or rate limiting. Keep port 8000 loopback-only and protect the public endpoint with
the calling backend's gateway, a private network, or an Nginx IP allow-list.

## 11. Install Certbot and issue the Let's Encrypt certificate

The tool is named Certbot, not certbox. Certbot recommends its snap package for most systems:

~~~bash
apt-get remove -y certbot || true    # Remove an older apt-installed Certbot to avoid conflicts.
snap install core                   # Install Snap's core runtime if it is absent.
snap refresh core                   # Update the Snap runtime before installing Certbot.
snap install --classic certbot      # Install the current Certbot package with required system access.
ln -s /snap/bin/certbot /usr/local/bin/certbot    # Make certbot available in the normal command path.
certbot --version                   # Confirm Certbot is installed and runnable.
~~~

If the symlink already exists, verify it points to /snap/bin/certbot instead of recreating it.

Ensure DNS resolves correctly and ports 80/443 are open, then request and install the certificate:

~~~bash
certbot --nginx -d herbalife-agent.gpisurveys.com --email YOUR_REAL_EMAIL@gpisurveys.com --agree-tos --no-eff-email --redirect    # Request a certificate, configure Nginx, and redirect HTTP to HTTPS.
nginx -t    # Revalidate Nginx after Certbot changes it.
systemctl reload nginx    # Load the TLS-enabled Nginx configuration.
curl -I https://herbalife-agent.gpisurveys.com/    # Confirm the HTTPS endpoint answers with valid TLS.
~~~

Test automatic renewal:

~~~bash
certbot renew --dry-run    # Simulate renewal without changing the real certificate.
systemctl list-timers --all | grep -i certbot    # Show the scheduled automatic renewal timer.
~~~

Official reference: [Certbot with Nginx instructions](https://certbot.eff.org/instructions?os=ubuntufocal&ws=nginx).

After Docker, Nginx, and Certbot are verified, return to section 6, apply the SSH hardening override, test a new deploy-user SSH session, and only then close the original root session.

## 12. Create the GitLab registry pull token

In GitLab:

1. Open Project > Settings > Repository > Deploy tokens.
2. Create a token named gitlab-deploy-token.
3. Give it read_registry only.
4. Set an expiry date and rotation reminder.
5. Record the username and token immediately.

When the token is named gitlab-deploy-token, GitLab exposes CI_DEPLOY_USER and CI_DEPLOY_PASSWORD to jobs. A read-only deploy token is appropriate for the server; the build job uses GitLab's short-lived CI_REGISTRY_USER and CI_REGISTRY_PASSWORD to push.

Reference: [GitLab deploy tokens](https://docs.gitlab.com/user/project/deploy_tokens/).

## 13. Capture and verify the SSH host key

On the server through the trusted Hetzner console or existing verified root session:

~~~bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub    # Print the trusted server key fingerprint.
cat /etc/ssh/ssh_host_ed25519_key.pub               # Print the public host key for manual comparison.
~~~

On a trusted administrator machine:

~~~bash
ssh-keyscan -p 22 -t ed25519 herbalife-agent.gpisurveys.com > herbalife-agent-known-hosts    # Capture the server's public host key into a CI-ready file.
ssh-keygen -lf herbalife-agent-known-hosts    # Display the captured fingerprint for comparison.
~~~

Compare the SHA256 fingerprint with the server output. They must match.

Do not run ssh-keyscan inside the CI job. That trusts whatever host answers during the deployment and defeats host verification.

## 14. Configure GitLab CI/CD variables

Open Project > Settings > CI/CD > Variables.

Add:

| Variable | Type | Protection | Value |
|---|---|---|---|
| SSH_PRIVATE_KEY | File | Protected, environment production | Contents of herbalife_agent_gitlab_deploy private key, ending with a newline |
| SSH_KNOWN_HOSTS | File | Protected, environment production | Verified herbalife-agent-known-hosts contents |
| DEPLOY_HOST | Variable | Protected | herbalife-agent.gpisurveys.com |
| DEPLOY_PORT | Variable | Protected | 22 |
| DEPLOY_USER | Variable | Protected | deploy |
| DEPLOY_PATH | Variable | Protected | /opt/herbalife-agent |
| PRODUCTION_URL | Variable | Protected | https://herbalife-agent.gpisurveys.com |
| CI_DEPLOY_USER | Predefined by deploy token | Protected | Deploy-token username |
| CI_DEPLOY_PASSWORD | Predefined by deploy token | Masked and protected | Deploy-token secret |

### Environment variables that must be changed

There are two separate groups of variables. Do not put GitLab deployment variables inside
`/opt/herbalife-agent/app.env`.

In **GitLab → Settings → CI/CD → Variables**, use:

~~~text
DEPLOY_HOST=herbalife-agent.gpisurveys.com
DEPLOY_PORT=22
DEPLOY_USER=deploy
DEPLOY_PATH=/opt/herbalife-agent
PRODUCTION_URL=https://herbalife-agent.gpisurveys.com
~~~

`DEPLOY_HOST` must be a normal Variable containing only the hostname—no `https://`, username,
port, slash, quotes, or spaces. `SSH_PRIVATE_KEY` and `SSH_KNOWN_HOSTS` remain File variables.

In the server's **`/opt/herbalife-agent/app.env`**, use the real application credentials:

~~~dotenv
OPENAI_API_KEY=replace-with-real-openai-key
DATABASE_URL=postgresql://readonly-user:password@database-host:5432/database-name
CHARTS_API_BASE_URL=https://charts-api.gpisurveys.com
CHARTS_STATS_EXTERNAL_ACCESS_SECRET=replace-with-real-charts-secret
HERBALIFE_EXTERNAL_ACCESS_SECRET=replace-with-output-from-openssl-rand-hex-32

# This API is called by another backend, so browser CORS access is disabled.
CORS_ORIGINS=
~~~

`HERBALIFE_EXTERNAL_ACCESS_SECRET` must contain at least 32 characters. Generate it with
`openssl rand -hex 32` and configure the identical value only in the authorized calling backend.
It is sent as `Authorization: Bearer <secret>` to `POST /ask` and
`DELETE /threads/{thread_id}`; `/health` intentionally remains unauthenticated.

`CORS_ORIGINS` does not restrict backend-to-backend HTTP requests; browsers enforce CORS. If a
browser frontend later calls the agent directly, replace the empty value with that frontend's exact
origin, such as `https://app.gpisurveys.com`. Do not use `*` after adding authentication.

GitLab requires multiline SSH private keys stored as File variables to end with a newline. File variables containing SSH keys may need Visible visibility because masked variables cannot contain whitespace; protect the variable and never print it.

Reference: [Using SSH keys with GitLab CI/CD](https://docs.gitlab.com/ci/jobs/ssh_keys/).

## 15. GitLab CI/CD pipeline

The canonical pipeline is `herbalife_agent/.gitlab-ci.yml`. It has three stages:

1. `validate` installs the pinned runtime requirements, compiles all Python modules, checks
   installed dependency consistency, parses both Compose files, imports the FastAPI app with
   non-secret placeholders, and syntax-checks the deployment script.
2. `build_image` uses rootless BuildKit, reuses a registry-backed build cache, and pushes the
   immutable `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA` image. It requires no privileged Docker daemon.
3. `deploy_production` is a serialized, manual default-branch job. It verifies the SSH host
   through the supplied known-hosts file, uploads deployment assets, logs in with the read-only
   registry token, and runs the health-gated deploy script. Health is checked through loopback on
   the server, so an Nginx IP allow-list does not have to admit GitLab runner addresses.

Because this repository currently has a GitHub origin, import or mirror it into GitLab before
expecting `.gitlab-ci.yml` and the GitLab Container Registry variables to exist. Protect the
default branch so unreviewed commits cannot trigger a production deployment.

GitLab official references:

- [Rootless BuildKit](https://docs.gitlab.com/ci/docker/using_buildkit/)
- [SSH keys and known hosts](https://docs.gitlab.com/ci/jobs/ssh_keys/)
- [Deploy tokens](https://docs.gitlab.com/user/project/deploy_tokens/)

If the self-managed runner blocks rootless user namespaces, configure it according to GitLab's
BuildKit guidance or use an approved Buildah/Buildx runner. Do not grant arbitrary shared jobs
privileged access merely to make the build pass.

After the first manual production deployment succeeds, change `when: manual` to
`when: on_success` only if automatic default-branch deployment is intended.

## 16. First deployment

Before running CI, confirm the app.env file already exists on the server.

**Run on:** the Hetzner server as `deploy`, before approving the first pipeline:

~~~bash
cd /opt/herbalife-agent    # Enter the production application directory.
ls -la                     # List normal and hidden deployment files with ownership.
test -r app.env            # Succeed only if deploy can read the required secret file.
docker compose version     # Confirm Compose is available in this deploy-user session.
~~~

Before the first pipeline, it is normal for `compose.production.yaml`, `deploy.sh`, and
`.compose.env` to be absent. GitLab creates or uploads them during deployment.

Next, push the repository to GitLab, run the default-branch pipeline, and manually approve
`deploy_production`.

After the pipeline succeeds, verify on the Hetzner server as `deploy`:

~~~bash
cd /opt/herbalife-agent    # Enter the directory containing the deployed Compose files.
docker compose --env-file .compose.env -f compose.production.yaml ps    # Show container state and health.
docker compose --env-file .compose.env -f compose.production.yaml logs --tail=200 flavorai    # Show the agent's latest 200 log lines.
curl -fsS http://127.0.0.1:8000/health    # Test FastAPI directly, bypassing DNS and Nginx.
~~~

Finally, run these from your development computer to verify the public endpoint:

~~~bash
curl -fsS https://herbalife-agent.gpisurveys.com/health    # Verify the public health response through TLS and Nginx.
curl -I https://herbalife-agent.gpisurveys.com/            # Display public response headers and redirects.
~~~

## 17. Rollback

The deployment script automatically restores the previously recorded image if the new container fails its health check.

For a manual rollback, find a known-good immutable image tag in GitLab and run:

~~~bash
ssh deploy@herbalife-agent.gpisurveys.com    # Open a production shell as deploy.
cd /opt/herbalife-agent                   # Enter the directory containing deploy.sh.
./deploy.sh registry.gitlab.com/GROUP/PROJECT:KNOWN_GOOD_COMMIT_SHA    # Replace production with the selected known-good image.
~~~

Never rely only on latest or production tags for rollback; mutable tags do not identify the deployed source revision.

If a database migration was applied, container rollback does not revert the schema. Follow the migration's reviewed rollback procedure or restore a tested database backup.

## 18. Operations and maintenance

### Status and logs

~~~bash
cd /opt/herbalife-agent    # Enter the production application directory.
docker compose --env-file .compose.env -f compose.production.yaml ps    # Show agent status, ports, and health.
docker compose --env-file .compose.env -f compose.production.yaml logs -f --tail=200 flavorai    # Follow agent logs, starting with the last 200 lines; Ctrl+C stops following.
docker stats    # Show live CPU, memory, network, and I/O usage; Ctrl+C exits.
journalctl -u docker -u nginx --since '1 hour ago'    # Show Docker and Nginx service logs from the last hour.
tail -f /var/log/nginx/access.log /var/log/nginx/error.log    # Follow both Nginx request and error logs; Ctrl+C exits.
~~~

### Configuration checks

~~~bash
nginx -t    # Validate Nginx syntax and referenced files.
sshd -t     # Validate SSH server configuration without reloading it.
ufw status verbose    # Show firewall rules and default policies.
fail2ban-client status sshd    # Show SSH jail status and banned addresses.
certbot certificates           # List managed certificates, names, and expiry dates.
certbot renew --dry-run         # Test the complete renewal process safely.
~~~

### Patch cycle

Take a snapshot or verified backup first:

~~~bash
apt-get update    # Refresh available OS and Docker package versions.
apt-get -y upgrade    # Install normal OS package upgrades.
apt-get install --only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin    # Upgrade only the installed Docker components.
test -f /var/run/reboot-required && cat /var/run/reboot-required    # Report whether upgraded packages require a reboot.
~~~

Schedule reboots rather than leaving a required kernel reboot indefinitely.

### Backups

At minimum, back up:

- The external database using its database-native backup tool.
- /opt/herbalife-agent/app.env through an encrypted secret-management process.
- Persistent application data under /opt/herbalife-agent/data.
- Nginx configuration under /etc/nginx.
- A record of deployed image SHAs.

Do not treat a Docker image or Hetzner snapshot as the only database backup. Test restoration periodically.

### Key and token rotation

1. Generate a new deployment key.
2. Add the new public key to authorized_keys.
3. Replace the protected SSH_PRIVATE_KEY File variable.
4. Run and verify one deployment.
5. Remove the old public key.
6. Rotate the GitLab deploy token separately and verify registry pulls.

## 19. Security checklist

- [ ] Root SSH login is disabled only after deploy key login succeeds.
- [ ] SSH password login is disabled.
- [ ] The deploy key is dedicated to this environment.
- [ ] SSH_PRIVATE_KEY and SSH_KNOWN_HOSTS are GitLab File variables.
- [ ] The server fingerprint was verified outside the CI job.
- [ ] The GitLab deploy token has read_registry only.
- [ ] Production variables and the production environment are protected.
- [ ] Only Nginx ports 80/443 are public.
- [ ] The container port is bound to 127.0.0.1.
- [ ] app.env is mode 0600 and is never generated in pipeline logs.
- [ ] Docker Compose uses an immutable commit-SHA image.
- [ ] Container health checks and automatic rollback are enabled.
- [ ] Docker and Nginx logs are bounded or rotated.
- [ ] Certbot renewal dry-run passes.
- [ ] Database and persistent-data restoration has been tested.
- [ ] Docker group access is understood to be root-equivalent.

## 20. Common failures

### CI reports host-key verification failed

Do not disable StrictHostKeyChecking. Re-verify the server fingerprint through Hetzner console, regenerate SSH_KNOWN_HOSTS from a trusted network, and update the File variable.

### SSH private key fails with libcrypto error

Ensure the GitLab File variable ends with a newline after the final private-key line.

### Docker permission denied for deploy

Confirm deploy belongs to docker, then start a new login session:

~~~bash
id deploy               # Show deploy's effective group memberships.
getent group docker     # Show every account currently allowed to control Docker.
~~~

### Nginx returns 502

~~~bash
curl -v http://127.0.0.1:8000/health    # Test the upstream directly and print connection details.
docker compose --env-file /opt/herbalife-agent/.compose.env -f /opt/herbalife-agent/compose.production.yaml ps    # Check whether the container is running and healthy.
docker compose --env-file /opt/herbalife-agent/.compose.env -f /opt/herbalife-agent/compose.production.yaml logs --tail=200 flavorai    # Inspect recent startup or runtime errors.
tail -n 100 /var/log/nginx/error.log    # Inspect the latest Nginx proxy errors.
~~~

### PostgreSQL connection times out after 10 seconds

Use this procedure when application logs contain errors like:

~~~text
[inventory-db-timing] connection_acquire=10009.7ms status=error
psycopg2.OperationalError: connection to server at "10.3.0.3", port 5432 failed: timeout expired
~~~

The application sets `DB_CONNECT_TIMEOUT_S=10` by default. A failure at almost exactly 10 seconds
means that the TCP handshake did not complete. It occurs before PostgreSQL checks the database,
role, password, `pg_hba.conf`, or SQL statement. Do not increase the timeout as a fix.

`POST /ask` may still appear as HTTP 200 because it is an NDJSON streaming endpoint: the status is
committed before a later error is written inside the stream. `GET /health` checks the application
process but intentionally does not probe PostgreSQL. Use `scripts/preflight.py` for live database
verification.

The private-network values used by this production environment are:

| Item | Value |
|---|---|
| Agent server private IP | `10.3.0.4` |
| PostgreSQL server private IP | `10.3.0.3` |
| Hetzner private-network gateway | `10.3.0.1` |
| Private-network range | `10.3.0.0/16` |
| PostgreSQL port | `5432` |
| PostgreSQL host private interface | `eth1` |

If Hetzner Console assigns different values in the future, use the Console/API assignments rather
than copying these values blindly.

#### 1. Confirm the container loaded the intended database target

Production credentials live in `/opt/herbalife-agent/app.env`. Prefer the PostgreSQL private IP in
`DATABASE_URL`; do not send traffic over the public IP when both servers share the Hetzner private
network.

**Run on:** the agent server.

~~~bash
cd /opt/herbalife-agent
docker compose --env-file .compose.env -f compose.production.yaml exec flavorai python scripts/preflight.py
~~~

Expected target:

~~~text
database target: 10.3.0.3:5432/production (connect timeout 10s)
~~~

If it still prints a public address, edit only the host portion of `DATABASE_URL`, preserving the
role, password, port, and database. Environment variables are fixed when a container is created;
`docker compose restart` does not reload `app.env`. Recreate the service:

~~~bash
cd /opt/herbalife-agent
docker compose --env-file .compose.env -f compose.production.yaml up -d --force-recreate flavorai
~~~

Never print the complete `DATABASE_URL` in logs or shell output because it contains the password.

#### 2. Test raw TCP from the agent container

**Run on:** the agent server.

~~~bash
ip route get 10.3.0.3    # Confirm the route selects private source 10.3.0.4.
cd /opt/herbalife-agent
docker compose --env-file .compose.env -f compose.production.yaml exec flavorai sh
~~~

Inside the container, start Python and make a socket connection:

~~~text
python
>>> import socket
>>> socket.create_connection(("10.3.0.3", 5432), 5)
~~~

Interpret the result:

- A socket object means TCP works; continue with PostgreSQL authentication checks.
- `TimeoutError` means a firewall, missing address, or route silently dropped the handshake.
- `ConnectionRefusedError` means the destination is reachable but nothing accepts that address and
  port.

Port `5433` is unrelated unless the configured DSN explicitly uses it.

#### 3. Verify containerized PostgreSQL

PostgreSQL runs in Docker on the database host. `sudo -u postgres psql` on the host can therefore
fail with `unknown user postgres`; that does not indicate a database fault.

**Run on:** the PostgreSQL server.

~~~bash
docker ps -a    # Identify the production postgres container, not Dokploy's internal database.
docker exec -it POSTGRES_CONTAINER_ID sh
~~~

Inside the PostgreSQL container:

~~~bash
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
~~~

At the `psql` prompt:

~~~sql
SHOW listen_addresses;
SHOW port;
SHOW hba_file;
SELECT line_number, type, database, user_name,
       address, auth_method, error
FROM pg_hba_file_rules
ORDER BY line_number;
~~~

The production incident showed `listen_addresses = '*'`, port `5432`, and a remote `host` rule
using `scram-sha-256`. A wrong password or missing HBA rule returns a PostgreSQL error after TCP
connects; it does not normally produce the 10-second socket timeout described here.

On the database host, confirm that Docker published the port:

~~~bash
sudo ss -ltnp | grep ':5432'
~~~

A `docker-proxy` listener on `0.0.0.0:5432` confirms the host is publishing the container port; it
does not by itself prove that packets reach the container.

#### 4. Observe the handshake and firewall path

**Run on:** the PostgreSQL server while repeating the socket test from the agent container.

~~~bash
sudo tcpdump -nn -i any 'host 10.3.0.4 and tcp port 5432'
~~~

Interpret the capture:

- No packets: inspect the agent host's route, egress filtering, and Hetzner Network attachment.
- Repeated incoming `Flags [S]` with no reply: the database host received SYN packets but dropped
  them before the handshake completed.
- `S`, `S.`, then `.`: TCP works; inspect credentials, TLS requirements, and `pg_hba.conf`.
- A reset response: the host is reachable but the listener/published-port path is wrong.

Docker DNAT can send published-port traffic through `FORWARD` and `DOCKER-USER`, so UFW `ALLOW IN`
rules alone are not complete evidence. Inspect counters without changing rules:

~~~bash
sudo iptables -vnL INPUT --line-numbers
sudo iptables -vnL ufw-user-input --line-numbers
sudo iptables -vnL FORWARD --line-numbers
sudo iptables -vnL DOCKER-USER --line-numbers
sudo iptables -t nat -vnL DOCKER --line-numbers
~~~

In this environment, allow the private agent source narrowly on TCP 5432, then confirm rule order:

~~~bash
sudo ufw allow proto tcp from 10.3.0.4 to any port 5432 comment 'agent private database access'
sudo ufw status numbered
~~~

Keep the specific allow before any blanket deny for TCP 5432. Do not disable UFW or remove the final
deny rule while diagnosing. Also remember that the source shown in the agent's Uvicorn access log,
such as `172.18.0.1`, is the incoming Docker bridge peer; it is not the source IP seen by the remote
PostgreSQL server. With Docker's default NAT bridge, PostgreSQL sees the agent host's selected
address, `10.3.0.4` for this private route.

#### 5. Detect a missing private address or return route

The resolved production incident had this exact failure: packets arrived on the PostgreSQL host's
private NIC, but the NIC had no IPv4 address and the host tried to return traffic through its public
interface.

**Run on:** the PostgreSQL server.

~~~bash
ip -4 addr show eth1
ip route get 10.3.0.4
ip -4 route show
~~~

Broken state observed during the incident:

~~~text
eth1 has no IPv4 address
10.3.0.4 via 172.31.1.1 dev eth0 src PUBLIC_DATABASE_IP
~~~

Healthy state:

~~~text
eth1 has 10.3.0.3/32
10.3.0.4 via 10.3.0.1 dev eth1 src 10.3.0.3
~~~

Confirm in Hetzner Console that `10.3.0.3` is assigned to this server before configuring it. For a
temporary, reboot-unsafe recovery:

~~~bash
sudo ip link set eth1 up
sudo ip link set eth1 mtu 1450
sudo ip addr add 10.3.0.3/32 dev eth1
sudo ip route add 10.3.0.0/16 via 10.3.0.1 dev eth1 onlink
~~~

Verify the route, then repeat the container socket test and preflight:

~~~bash
ip -4 addr show eth1
ip route get 10.3.0.4
~~~

These `ip` changes disappear after reboot.

#### 6. Persist the private interface with Netplan

Create `/etc/netplan/60-private-network.yaml` on the PostgreSQL host. Keep the existing public
interface configuration in its existing Netplan file; this new file only adds `eth1`.

~~~yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    eth1:
      addresses:
        - 10.3.0.3/32
      mtu: 1450
      routes:
        - to: 10.3.0.0/16
          via: 10.3.0.1
          on-link: true
~~~

Apply cautiously over SSH:

~~~bash
sudo chmod 600 /etc/netplan/60-private-network.yaml
sudo netplan generate
sudo netplan try
~~~

Accept the configuration only after confirming the public SSH session and private route still work.
`netplan try` rolls back automatically if it is not confirmed. Do not reboot until the persistent
configuration has been accepted. Hetzner's official manual configuration uses the assigned private
address as `/32`, MTU 1450, and an on-link route through the private-network gateway; always verify
the current assignment in Hetzner Console first.

#### 7. Recover Netplan when udev cannot reload

If Netplan reports either of these errors:

~~~text
Failed to send reload request: No such file or directory
Failed to allocate directory watch: Too many open files
~~~

check the host limits before changing them:

~~~bash
sysctl fs.inotify.max_user_instances
sysctl fs.inotify.max_user_watches
cat /proc/sys/fs/file-nr
sudo systemctl status systemd-udevd.service --no-pager -l
~~~

During the incident, the global file table was not exhausted, but
`fs.inotify.max_user_instances=128` was too low for this Docker host. The working recovery was:

~~~bash
sudo sysctl -w fs.inotify.max_user_instances=1024
sudo sysctl -w fs.inotify.max_user_watches=524288
sudo systemctl reset-failed systemd-udevd.service
sudo systemctl restart systemd-udevd-control.socket
sudo systemctl restart systemd-udevd-kernel.socket
sudo systemctl restart systemd-udevd.service
sudo systemctl is-active systemd-udevd.service
sudo udevadm control --reload
~~~

Persist the limits in `/etc/sysctl.d/99-inotify-limits.conf`:

~~~conf
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 524288
~~~

After udev is active and reload succeeds, retry `netplan generate` and `netplan try`. If udev still
fails, do not keep increasing limits blindly; inspect the detailed service journal and its file
descriptor limit:

~~~bash
sudo journalctl -u systemd-udevd.service -n 50 --no-pager -l
sudo systemctl show systemd-udevd.service -p LimitNOFILE
~~~

#### 8. Final verification

**Run on:** the agent server.

~~~bash
cd /opt/herbalife-agent
docker compose --env-file .compose.env -f compose.production.yaml exec flavorai python scripts/preflight.py
docker compose --env-file .compose.env -f compose.production.yaml logs --tail=100 flavorai
~~~

Success must include:

~~~text
database target: 10.3.0.3:5432/production
live database: OK
~~~

Schedule a controlled reboot later and re-run `ip -4 addr show eth1`,
`ip route get 10.3.0.4`, and preflight to prove the Netplan and sysctl files survive boot.

### Let's Encrypt validation fails

Verify DNS A/AAAA, Hetzner firewall, UFW, port 80, Nginx configuration, and any upstream proxy. Remove a broken AAAA record if IPv6 is not actually served.

### AI streaming stops around 60 seconds

Confirm `proxy_buffering` is off, `proxy_read_timeout` is long enough, and the calling backend reads
the NDJSON body incrementally instead of waiting for the complete response.

## Source references

- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker Compose plugin](https://docs.docker.com/compose/install/linux/)
- [GitLab rootless BuildKit](https://docs.gitlab.com/ci/docker/using_buildkit/)
- [GitLab build and push container images](https://docs.gitlab.com/user/packages/container_registry/build_and_push_images/)
- [GitLab SSH keys in CI/CD](https://docs.gitlab.com/ci/jobs/ssh_keys/)
- [GitLab deploy tokens](https://docs.gitlab.com/user/project/deploy_tokens/)
- [Nginx proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- [Certbot Nginx instructions](https://certbot.eff.org/instructions?os=ubuntufocal&ws=nginx)
- [Hetzner Cloud Firewalls](https://docs.hetzner.com/cloud/firewalls/overview/)
- [Hetzner Cloud Networks server configuration](https://docs.hetzner.com/networking/networks/server-configuration/)
- [Docker packet filtering and firewalls](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [PostgreSQL connection settings](https://www.postgresql.org/docs/current/runtime-config-connection.html)
- [PostgreSQL client authentication](https://www.postgresql.org/docs/current/auth-pg-hba-conf.html)
