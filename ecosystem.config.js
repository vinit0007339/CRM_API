// PM2, mirroring emailMarketingAPI/ecosystem.config.js so the droplet's deploy
// story is one story: `pm2 startOrRestart ecosystem.config.js`.
//
// A SECOND PROCESS BESIDE THE PLATFORM, not a replacement for it. nginx routes
// /api/v1/crm/ here; everything else still goes to the platform.
//
// ⚠️ CHECK `cwd` BEFORE THE FIRST DEPLOY. It is a sibling of the platform,
// whose own PM2 file says /var/cylonmarketing-test/emailMarketingAPI — but
// CRM_API has never been deployed, so this path is inferred, not observed. Get
// it wrong and PM2 starts in a directory that does not exist and restart-loops
// with "no such file or directory", which reads like a broken build rather
// than a wrong path. An earlier version of this file guessed
// /var/emailMarketingAPI-stage/ and was wrong.
module.exports = {
  apps: [
    {
      name: "crmApi-stage",
      script: "bash",
      // Same entry point as the platform: `dev_api.py` reads the port from
      // [CRM] port, prints the URL, and binds 127.0.0.1.
      //
      // ⚠️ IT ALSO TURNS THE RELOADER OFF HERE, because CRM_ENV below is not
      // "dev". The platform's PM2 entry runs its dev_api.py with reload=True
      // in production, which leaves a watcher process polling the tree for
      // changes that never arrive; this service keys the reloader on the
      // environment so one command is right in both places.
      args: "-c 'poetry run python dev_api.py'",
      cwd: "/var/cylonmarketing-test/CRM_API",
      interpreter: "none",
      autorestart: true,
      // ⚠️ NO `instances` / `exec_mode`, SO THIS IS ONE PROCESS — and that is
      // load-bearing, not a default left alone. [CRM] task_reminders may be
      // true in exactly one process: two schedulers both poll every minute,
      // and the only thing between that and a customer getting two reminder
      // emails is the conditional update on reminder.sent_at. If you ever add
      // `instances: 2`, move the scheduler out of the web process first.
      env: {
        PYTHONPATH: "/var/cylonmarketing-test/CRM_API",
        // Drives [CRM] environment: no reloader, and /health reports it.
        // Production needs its own app entry with CRM_ENV: "production" and
        // its own cwd — this file describes stage only, same as the
        // platform's.
        CRM_ENV: "stage",
      },
    },
  ],
};
