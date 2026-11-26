import os

import uvicorn

from .main import create_app

PORT = int(os.getenv("PF_PORT", "8765"))
app = create_app(PORT)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info", access_log=False)
