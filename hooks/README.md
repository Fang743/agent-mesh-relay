# Git Server Hooks

Install `pre-receive` into your coordination bare repository:

- Copy `hooks/pre-receive` into `<bare-repo>/hooks/pre-receive` and run `chmod +x`.
- Set `git --git-dir=<bare-repo> config agentmesh.leasedb /path/to/leases.db`.
