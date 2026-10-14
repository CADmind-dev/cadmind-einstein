// CADmind einstein under pm2. Install pm2 (npm install -g pm2), adjust the placeholders, then:
//   pm2 start deploy/pm2/ecosystem.config.js --only einstein-validator   (or einstein-miner)
//   pm2 save && pm2 startup
// Keep the token and key files mode 0600. Remove --dry-run-weights only when the validator should
// write weights and report commitments on chain. The optional updater app is opt-in (see README).
const HOME = process.env.HOME;
const VENV = `${HOME}/cadmind-einstein/.venv/bin`;

module.exports = {
  apps: [
    {
      name: "einstein-validator",
      script: `${VENV}/cadmind-einstein`,
      interpreter: "none",
      args: [
        "--state-dir", `${HOME}/.local/share/cadmind-einstein`,
        "--host", "127.0.0.1", "--port", "8793",
        "--token-file", `${HOME}/.config/cadmind-einstein/desk.token`,
        "--platform", "https://cadmind.ai",
        "--operator-key-file", `${HOME}/.config/cadmind-einstein/operator.key`,
        "--mint-key-file", `${HOME}/.config/cadmind-einstein/mint.key`,
        "--queue-key-file", `${HOME}/.config/cadmind-einstein/queue.key`,
        "--wallet.name", "VALIDATOR_WALLET", "--wallet.hotkey", "VALIDATOR_HOTKEY_NAME",
        "--subtensor.network", "test", "--netuid", "NETUID",
        "--trusted-proxy", "127.0.0.1",
        "--dry-run-weights",
      ],
      env: { PYTHONUNBUFFERED: "1" },
      restart_delay: 10000,
    },
    {
      name: "einstein-miner",
      script: `${VENV}/cadmind-einstein-miner`,
      interpreter: "none",
      args: [
        "--wallet.name", "MINER_WALLET", "--wallet.hotkey", "MINER_HOTKEY_NAME",
        "--subtensor.network", "test", "--netuid", "NETUID",
        "--validator", "https://VALIDATOR_HOST", "--validator-hotkey", "VALIDATOR_SS58",
        "--platform", "https://cadmind.ai",
        "--state", `${HOME}/.local/share/cadmind-einstein-miner`,
      ],
      env: { PYTHONUNBUFFERED: "1" },
      restart_delay: 30000,
    },
    {
      // Opt-in: checks for a newer tagged release every 30 minutes, installs it and restarts the
      // named app. Start it only if you want automatic updates.
      name: "einstein-updater",
      script: `${HOME}/cadmind-einstein/scripts/auto_update.sh`,
      interpreter: "bash",
      args: ["--restart", "pm2 restart einstein-validator", "--loop", "1800"],
      autorestart: true,
    },
  ],
};
