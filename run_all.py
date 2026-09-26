"""Run the full ForkCast pipeline: data -> warehouse -> forecast -> growth -> dashboard."""
import subprocess, sys, time
STEPS = ["generate_data.py", "build_warehouse.py", "forecast.py", "growth.py", "export_dashboard.py"]
for step in STEPS:
    t = time.time()
    print(f"\n=== {step}")
    subprocess.run([sys.executable, step], cwd="src", check=True)
    print(f"    done in {time.time() - t:.1f}s")
