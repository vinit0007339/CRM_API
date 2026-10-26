// PM2, mirroring the monolith's ecosystem.config.js so the droplet's deploy
// story does not change: `pm2 startOrRestart ecosystem.config.js`.
//
// A SECOND PROCESS BESIDE THE MONOLITH, not a replacement for it. During the
// Phase 5 dual-run both serve the CRM and nginx still points at the monolith;
// Phase 6 is one nginx rule and a reload, and the rollback is the same reload
// backwards.
module.exports = {
  apps: [
    {
      name: "crm-service-stage",
      // ⚠️ `cwd` AND `PYTHONPATH` BELOW ARE PATHS ON THE DROPLET, and they
      // were renamed with this repo (crm-service-api → CRM_API). The
      // directory on the server has to be renamed to match, or PM2 starts in
      // a path that does not exist and the app restart-loops with
      // "no such file or directory" — which looks like a broken build.
      // If you are not renaming it on the server, change these two back.
      script: "bash",
      args: "-c 'poetry run uvicorn app.main:app --host 127.0.0.1 --port 8100'",
      cwd: "/var/emailMarketingAPI-stage/CRM_API",
      interpreter: "none",
      autorestart: true,
      // Liveness is /health, which touches nothing. If this ever watches
      // /health/ready instead, a Mongo blip becomes a restart loop — see the
      // note above the two endpoints in app/main.py.
      env: {
        PYTHONPATH: "/var/emailMarketingAPI-stage/CRM_API",
        CRM_ENV: "stage",
      },
    },
  ],
};
