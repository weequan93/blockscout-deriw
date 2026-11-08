# DeriW testnet

Use this directory for testnet's own `.env` and `envs/*.env` files. Setup,
startup and release commands are in [the environment-directory guide](../deploy/compose/README.md).
Populate the chain ID and settings from the existing testnet installation.
The original `docker-compose/` defaults are independent of this directory.

On the server, run `bash ../deploy/compose/setup.sh` once, then fill `.env`,
`envs/*.env` and `docker-compose.local.yml`. These local values are ignored by Git;
the Compose definitions, samples and start command are versioned.

From the backend repository, start testnet with:

```bash
./docker-compose.test/start.sh --build
```

Use `./start.sh --build` from this directory, or `--no-build` to use selected images.
The command validates configuration before starting containers.
