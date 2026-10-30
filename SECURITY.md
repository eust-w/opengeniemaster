# Security Policy

> 中文：请不要在公开 Issue 里报告安全漏洞，请改用下文的 GitHub 私密报告渠道。

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Reporting a vulnerability

Report vulnerabilities privately through GitHub. On the repository's **Security** tab, choose **Report a vulnerability**, or open this link:

<https://github.com/eust-w/opengeniemaster/security/advisories/new>

Please do not open public issues, discussions, or pull requests for security problems.

A helpful report includes:

- the affected version or commit;
- the component, for example the MCP server, the robot-side HTTP execution service, shared admission, memory admission, or persistence in the state directory;
- steps to reproduce and the impact.

This is a volunteer-maintained project. The maintainers will acknowledge your report as soon as they can, keep you informed while a fix is prepared, and credit you in the advisory unless you prefer otherwise.

## Scope

These count as security issues:

- ways to make the harness send robot commands that admission should reject: commands to a stopped robot, commands that bypass the person-in-workspace interlock, or commands that skip step dependencies;
- ways to plant or change long-term memory so that it weakens safety behaviour;
- authentication or input-validation flaws in the robot-side HTTP execution service or the MCP server;
- path traversal or similar problems in the state directory, event logs, or replay files.

These are out of scope:

- vulnerabilities in third-party robot stacks, policies, or services that you connect to OpenGenieMaster;
- results of the semantic simulator that differ from real-world behaviour (please open a normal issue).

## Physical safety

OpenGenieMaster can send commands to real robots, but it is research software and has not been verified on real hardware. Never rely on it as the only safety layer. Keep hardware emergency stops, human supervision, and motion limits in place.

The robot-side HTTP service binds to `127.0.0.1` by default and supports bearer tokens. Run it only on trusted networks, and set a token whenever it listens on anything other than localhost.
