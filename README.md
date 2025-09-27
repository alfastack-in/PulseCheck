### Pulse Check

Automated Goal & Progress Tracking in Slack

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app pulsecheck
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/pulsecheck
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### Verifying Slack Jobs

After configuring the *PulseCheck Settings* doctype with your preferred schedule, the scheduler entries can be exercised manually with Bench:

```bash
bench --site <your-site> execute pulsecheck.pulse_check.prompts.enqueue_weekly_prompts
bench --site <your-site> execute pulsecheck.pulse_check.digests.enqueue_weekly_digest
```

Both commands honour the enable flag, scheduled weekday, notification time, and Slack bot token. They exit gracefully without sending messages when any of the prerequisites are missing.


### License

mit
