# paperbot

Daily research digest: harvest new papers (arXiv, OpenAlex), score abstracts with Jev,
debate the relevant ones with Claude (advocate vs skeptic, judge with veto), and write a
Markdown digest of what's worth reading and why.

```bash
pip install -e ".[dev]"
cp .env.example .env   # add TYPESAFE_API_KEY, OPENALEX_API_KEY, ANTHROPIC_API_KEY
pytest
paperbot run
```

Runs daily in the cloud (GitHub Actions + Cloudflare Pages): see `DEPLOY.md`.
All commands: see `cheatsheet.md`. Edit `config/profile.yaml` to change what counts as relevant. See `CLAUDE.md` for the
architecture, design decisions, status, and next steps.
