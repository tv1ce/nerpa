import uvicorn
from app.database import init_db

if __name__ == "__main__":
    init_db()
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=False)
