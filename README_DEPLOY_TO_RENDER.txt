MONTEZ LIQUOR — RENDER DEPLOYMENT

This is a FREE-plan deployment package for initial testing only.
It is the MONTEZ LIQUOR app, not the full YES WE KEN dashboard.

IMPORTANT FREE-PLAN LIMITATION
Render Free web services do not support persistent disks. This package stores SQLite data under the configured MONTEZ_DATA_DIR (currently /tmp/kaka-liquor-data), which is temporary. Data can be lost when the service restarts, sleeps, or redeploys. Do not use this free deployment for real sales, stock, customer credit records, or business accounting until the database is moved to persistent/managed storage and tested.

UPLOAD TO GITHUB
1. Download and extract this ZIP on your computer.
2. Open https://github.com and sign in.
3. Create a NEW PRIVATE repository named montez-liquor-render.
4. In the repository, choose Add file > Upload files.
5. Upload the files inside this extracted folder (not the ZIP). Make sure render.yaml and kaka_liquor_app.py are at the repository root.
6. Commit the upload.

DEPLOY TO RENDER
1. Open https://render.com and sign in.
2. Click New + > Blueprint.
3. Connect GitHub if prompted, then choose montez-liquor-render.
4. Render reads render.yaml. Confirm the service plan is Free.
5. Set MONTEZ_INITIAL_ADMIN_PASSWORD (legacy KAKA_INITIAL_ADMIN_PASSWORD is also accepted) to a new password of at least 12 characters when prompted. Keep it private.
6. Apply/deploy. Wait for the build to finish.
7. Open the .onrender.com URL shown by Render.
8. Log in with username admin and the password you set.

TROUBLESHOOTING
- If health check fails, open Render Logs and look for the first error.
- Do not add real customer or financial data to the free test deployment.
- The package does not include the database from your laptop; migrating existing records is a separate task.
- M-Pesa and all business functions must be tested after the service starts. This package has not been verified in a live Render deployment.
