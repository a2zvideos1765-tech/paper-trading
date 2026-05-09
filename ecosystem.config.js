// PM2 ecosystem for paper-trading.
// Usage:
//   pm2 start ecosystem.config.js
//   pm2 save
//   pm2 startup     (once, to auto-start on reboot)
//
// Each app loads .env (PM2 doesn't, so we shell out through bash -lc which sources it
// via python-dotenv inside the Python entrypoints).
//
// The poller and trader sleep outside market hours, so they're cheap when idle —
// no need for cron_restart on those.

module.exports = {
  apps: [
    {
      name: "paperaglo-web",
      script: "python",
      args: "-m src.runners.web",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 20,
      restart_delay: 5000,
      out_file: "logs/pm2/web.out.log",
      error_file: "logs/pm2/web.err.log",
      merge_logs: true,
    },
    {
      name: "paperaglo-poller",
      script: "python",
      args: "-m src.runners.poller",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 50,
      restart_delay: 10000,
      out_file: "logs/pm2/poller.out.log",
      error_file: "logs/pm2/poller.err.log",
      merge_logs: true,
    },
    {
      name: "paperaglo-trader",
      script: "python",
      args: "-m src.runners.trader",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 50,
      restart_delay: 10000,
      out_file: "logs/pm2/trader.out.log",
      error_file: "logs/pm2/trader.err.log",
      merge_logs: true,
    },
    {
      name: "paperaglo-backfill",
      script: "python",
      args: "-m src.runners.backfill",
      cwd: __dirname,
      autorestart: false,
      // Run weekdays at 16:30 IST (after market close 15:30 + buffer).
      // PM2's cron_restart fires *restart* on the schedule — combined with
      // autorestart:false the script runs once per fire and exits cleanly.
      cron_restart: "30 16 * * 1-5",
      out_file: "logs/pm2/backfill.out.log",
      error_file: "logs/pm2/backfill.err.log",
      merge_logs: true,
    },
  ],
};
