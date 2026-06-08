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
    {
      // Drains backfill_queue (rows enqueued when users add symbols via /symbols).
      // Runs every day at 18:00 IST after the regular nightly backfill, paced at
      // ~1 fetch/sec to stay below Angel's rate limit. Stops at 06:00 IST.
      name: "paperaglo-backfill-queue",
      script: "python",
      args: "-m src.runners.backfill_queue",
      cwd: __dirname,
      autorestart: false,
      cron_restart: "0 18 * * *",
      out_file: "logs/pm2/backfill_queue.out.log",
      error_file: "logs/pm2/backfill_queue.err.log",
      merge_logs: true,
    },
    {
      // Refreshes the Angel One instrument master (~80k rows) once a week.
      // The list barely changes between corporate actions; weekly is plenty.
      // Manual refreshes are also possible via the "Refresh now" button on /symbols.
      name: "paperaglo-instruments",
      script: "python",
      args: "-m tools.refresh_instruments",
      cwd: __dirname,
      autorestart: false,
      cron_restart: "0 3 * * 0",
      out_file: "logs/pm2/instruments.out.log",
      error_file: "logs/pm2/instruments.err.log",
      merge_logs: true,
    },
    {
      // Real-money trading runner (Angel One). Places CNC LIMIT orders at the
      // engine's decided price for the live S404 portfolio. The master kill switch
      // (real_bot_state.enabled) defaults OFF on deploy — flip it on /bot when ready.
      // Always runs (shadows funds/holdings even when the bot is OFF).
      name: "paperaglo-real-trader",
      script: "python",
      args: "-m src.runners.real_trader",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 50,
      restart_delay: 15000,
      out_file: "logs/pm2/real_trader.out.log",
      error_file: "logs/pm2/real_trader.err.log",
      merge_logs: true,
    },
    {
      // MCP server — allows Claude to read/debug the platform and make minor writes
      // (add/remove symbols, toggle bot, tweak strategy params). No order placement.
      // Requires MCP_TOKEN in .env. Reverse-proxy with Caddy (see Caddyfile.example).
      name: "paperaglo-mcp",
      script: "python",
      args: "-m src.mcp.server",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 20,
      restart_delay: 10000,
      out_file: "logs/pm2/mcp.out.log",
      error_file: "logs/pm2/mcp.err.log",
      merge_logs: true,
    },
  ],
};
