# Contributing to GRINCH-GRAM

First off, thanks for taking the time to contribute!

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/GRINCH-GRAM.git`
3. Create a feature branch: `git checkout -b feat/amazing-feature`
4. Make your changes
5. Commit using [Conventional Commits](https://www.conventionalcommits.org/)
6. Push and open a Pull Request

## Commit Message Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/) v1.0.0.

### Allowed Types

| Type | When to use | Example |
|------|-------------|---------|
| `feat:` | New feature | `feat: add AI Brain tabs to dashboard` |
| `fix:` | Bug fix | `fix: prevent overflow:hidden on mobile` |
| `docs:` | Documentation only | `docs: update deployment guide` |
| `style:` | Code style (formatting, no logic change) | `style: format with black` |
| `refactor:` | Code refactoring | `refactor: extract chart colors to CSS vars` |
| `perf:` | Performance improvement | `perf: remove global * { transition }` |
| `test:` | Adding or correcting tests | `test: add unit tests for grid trader` |
| `ci:` | CI/CD, automation, bot fixes | `ci(auto-fix): format code with ruff` |
| `build:` | Build system or dependencies | `build: bump flask to 3.x` |
| `chore:` | Maintenance, no src change | `chore: update .gitignore` |
| `revert:` | Revert previous commit | `revert: remove test artifact from .env` |

### Bot Commits

All automated commits must use `ci(auto-fix):` or `ci(audit):` prefix:

```
ci(auto-fix): format code with ruff & black [skip ci]
ci(audit): nightly security & health check [skip ci]
```

**Never use** `robot:`, `overlord:`, or `bot:` — these are not standard Conventional Commit types.

## Before Submitting

- [ ] `ruff check .` passes without errors
- [ ] `pytest` passes (if tests exist for your change)
- [ ] Bandit security scan shows no high-severity issues
- [ ] You have filled out the PR template completely
- [ ] Your branch is up to date with `main`

## Code Style

- Python 3.11+ with type hints where possible
- Line length: 100 characters (enforced by Ruff)
- Follow PEP 8 guidelines
- Write docstrings for public functions and classes

## Reporting Bugs

Use the [Bug Report](https://github.com/Alexkkkkk/GRINCH-GRAM/issues/new?template=bug_report.yml) template. Include:

- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Python version)
- Relevant logs or screenshots

## Requesting Features

Use the [Feature Request](https://github.com/Alexkkkkk/GRINCH-GRAM/issues/new?template=feature_request.yml) template. Describe:

- The problem you're trying to solve
- Your proposed solution
- Any alternatives you've considered

## Questions?

Open a [Discussion](https://github.com/Alexkkkkk/GRINCH-GRAM/discussions) or reach out via Telegram if configured.
