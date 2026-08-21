# Transit PWA AWS Deployment

This repository is now used for the Transit PWA migration.

Current deploy artifact: `transit-pwa-aws-ready-v2.zip`

AWS target: `/home/ubuntu/transit-pwa`

Secrets such as `DATA_GO_KR_KEY` must stay only in the server-side `.env` and must not be committed to GitHub.

Deployment flow:
1. Download the ZIP from this repository.
2. Extract it to `/home/ubuntu/transit-pwa`.
3. Create `.env` from `.env.example` and set `DATA_GO_KR_KEY` only on the server.
4. Create a Python virtual environment and install `requirements.txt`.
5. Install and enable `deploy/transit-web.service` and `deploy/transit-collector.service`.
