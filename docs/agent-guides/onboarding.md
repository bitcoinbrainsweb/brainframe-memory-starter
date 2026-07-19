# Onboarding Guide

## Prerequisites

- GitHub account + git installed locally
- Supabase account (free tier at supabase.com)
- Claude Pro or Team plan (Free plan context limits make boot fetching unreliable)
- bash + curl (macOS/Linux built-in; Windows: use WSL)

---

## Owner setup (under 2 hours)

### Step 1: Fork this repo

Click **Use this template** on GitHub and create your own repo. Name it something like `{your-name}-memory`.

### Step 2: Clone locally

```bash
git clone https://github.com/{your-username}/{your-repo}.git
cd {your-repo}
```

### Step 3: Create a Supabase project

1. Go to supabase.com > New project
2. Choose any region
3. Save your project URL and anon key (you will need them in Step 4)

### Step 4: Run onboarding

```bash
bash scripts/onboard.sh
```

The script prompts for your Supabase URL and anon key, stores them at `~/.config/memory-starter/.env` (outside the project), runs the database migration, and smoke-tests connectivity.

If the auto-migration fails (some Supabase plans restrict it), follow the manual migration prompt: paste the SQL from `scripts/migrations/001-initial-schema.sql` into the Supabase SQL editor.

### Step 5: Customize your USER/ files

Edit:
- `USER/routing/facts.md` -- replace `{YOUR_NAME}` etc. with your actual values
- `USER/routing/preferences.md` -- adjust to your communication style
- `USER/people.md` -- add collaborators, clients, contacts

### Step 6: Paste project instructions into Claude

1. Open Claude > create a new Project
2. Open `SYSTEM/PROJECT_INSTRUCTIONS_OWNER.md`
3. Replace `{YOUR_GITHUB_USER}` and `{YOUR_REPO}` with your values
4. Paste the whole file into the Project instructions field

### Step 7: Test

Open a new chat in the Project. Type: `boot`

Claude should fetch T2 files and greet you. If it fails, check that the GitHub URLs in your Project instructions are correct and the repo is public (or Claude has access).

---

## Contributor setup (under 1 hour)

1. Owner grants you read access to the repo (or repo is public)
2. Clone the repo
3. Create a new Claude Project
4. Paste `SYSTEM/PROJECT_INSTRUCTIONS_CONTRIBUTOR.md` into the Project instructions (replace placeholders)
5. Test: type `contribute: test note about {any topic}`
6. Claude should write a file to `contributions/` for Owner to review

---

## Reader setup (under 30 minutes)

1. Owner grants you read access (or repo is public)
2. Create a new Claude Project
3. Paste `SYSTEM/PROJECT_INSTRUCTIONS_READER.md` into the Project instructions (replace placeholders)
4. Test: type `find all decisions about {any topic}`

---

## Receiving updates

This repo is a template you own. Upstream updates arrive as PRs if you have set up the sync workflow.

When a PR arrives from upstream:
- Review the changes (they will only touch `SYSTEM/` files)
- Merge if the changes improve your setup
- `USER/` files are never touched by upstream PRs

If you want to pull updates manually:
```bash
git remote add upstream https://github.com/YOUR_GITHUB_USER/YOUR_REPO.git
git fetch upstream
git merge upstream/main --allow-unrelated-histories
```

---

## Windows / WSL note

The onboarding script requires bash. On Windows, use WSL2 or Git Bash. Alternatively, follow the manual steps:

1. Create `~/.config/memory-starter/.env` manually with your Supabase credentials
2. Run the SQL migration in the Supabase SQL editor directly
3. Skip to Step 5 above
