"""Run the production Controller API."""

from vonk_control.api.production import production_app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(production_app(), host="0.0.0.0", port=8000, access_log=False)
