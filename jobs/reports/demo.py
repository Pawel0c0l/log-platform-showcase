import os
import time

def run(client, run_id: str, params: dict):
    """
    Demo job:
      - logs steps
      - creates a small artifact file
      - uploads artifact to log-platform
    """

    source = "jobs.reports.demo"
    client.log("INFO", "SCRIPT", source, "Step 1/3: start", run_id=run_id, context={"step": "start"})

    time.sleep(0.2)

    client.log("INFO", "SCRIPT", source, "Step 2/3: generating artifact", run_id=run_id, context={"step": "generate"})

    out_path = "/tmp/demo_result.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("demo result\n")
        f.write(f"params={params}\n")

    artifact_id = client.upload_artifact(out_path, kind="REPORT", run_id=run_id)
    client.log("INFO", "SCRIPT", source, "Artifact uploaded", run_id=run_id, context={"artifact_id": artifact_id})


    client.log("INFO", "SCRIPT", source, "Step 3/3: done", run_id=run_id, context={"step": "done"})
