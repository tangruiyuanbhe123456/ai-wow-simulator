# Push to GitHub — 推到 tangruiyuanbhe123456/ai-wow-simulator

Two artifacts are ready at the project root:

- `ai-wow-simulator.bundle`  (66 KB) — full git history with all 5 commits
- `ai-wow-simulator.zip`     (68 KB) — same source tree as the latest commit

`gh auth login` OAuth device-flow times out on this host (corporate firewall
blocks `github.com/login/oauth/access_token` but NOT the main `github.com`
hostname — that's why direct `curl https://github.com` works but gh auth
doesn't). So we ship the repo as artifacts and let you push from any
network-reachable environment.

---

## Option A — clone from the bundle (preserves the 5-commit history)

```bash
# 1. Copy the bundle file to a machine with push access to GitHub.
#    (USB stick, network share, scp, etc.)

# 2. On that machine:
mkdir ai-wow-simulator && cd ai-wow-simulator
git clone ../ai-wow-simulator.bundle .

# 3. Confirm the 5-commit history is intact:
git log --oneline
# 328d6b7 feat: 5v5 arena (Honor-of-Kings-inspired) + Dockerfile
# 64f8e2b fix: mock agent mana rotation - never crashes on low MP
# 13bcf81 feat: e2e verified — difficulty PASS, ...
# 3651d05 feat: db schema+store + scripts + docs + Makefile + start.bat
# 3cc531d chore: init project skeleton + .gitignore + Obsidian note

# 4. Create the empty repo on github.com (Public or Private, no README/license).
#    https://github.com/new — name: ai-wow-simulator
#    DO NOT tick "Add a README file" / "Add .gitignore" / "Choose a license"
#    — those would conflict with the bundle's existing files.

# 5. Push:
git remote add origin https://github.com/tangruiyuanbhe123456/ai-wow-simulator.git
git push -u origin master
# (default branch on this bundle is `master`, not `main`)
```

If you prefer the default branch to be `main`:

```bash
git branch -m master main
git push -u origin main
```

---

## Option B — unzip + fresh init (loses the 5-commit history but keeps source)

Use this if you don't have git installed on the destination machine and just
want the latest snapshot.

```bash
unzip ai-wow-simulator.zip -d ai-wow-simulator/
cd ai-wow-simulator
git init
git add -A
git commit -m "feat: 5v5 arena (Honor-of-Kings-inspired) + Dockerfile"
# (single commit, no granular history)
git remote add origin https://github.com/tangruiyuanbhe123456/ai-wow-simulator.git
git push -u origin master
```

---

## Authentication for `git push`

`https://github.com/...` pushes prompt for username + password. GitHub has
deprecated password auth; you need a **Personal Access Token (PAT)**:

1. Open https://github.com/settings/tokens
2. "Generate new token" → "Generate new token (classic)"
3. Scopes: tick **`repo`** (full repo access)
4. Expiration: your choice (90 days / No expiration)
6. Copy the token (`ghp_...`)
7. When `git push` prompts:
   - Username: `tangruiyuanbhe123456`
   - Password: paste the `ghp_...` token

Or save it once so you don't re-prompt:

```bash
git config --global credential.helper store
git push -u origin master   # prompts once, saves to ~/.git-credentials
```

---

## Verifying the push landed

```bash
curl -s https://api.github.com/repos/tangruiyuanbhe123456/ai-wow-simulator
# Should return JSON with the 5-commit history and default_branch.
```

---

## Cleanup on the source machine

The bundle and zip live at `D:\Projects\ai-wow-simulator\`. After a
successful push you can delete them or keep them as a cold backup:

```bash
rm D:\Projects\ai-wow-simulator\ai-wow-simulator.bundle
rm D:\Projects\ai-wow-simulator\ai-wow-simulator.zip
```

(They are excluded by `.gitignore` already — won't show up in `git status`.)