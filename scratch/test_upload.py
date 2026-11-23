import sys
import os
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))

import asyncio
from agent.client import AgentNetworkClient
from agent.capture import ScreenCapture
from agent.compressor import ImageCompressor

async def main():
    c = AgentNetworkClient()
    cap = ScreenCapture()
    comp = ImageCompressor()

    print("Enrolling...")
    enrolled = await c.enroll_if_needed(force=True)
    print("Enrolled:", enrolled)

    img = cap.capture_primary()
    data = comp.compress_for_upload(img)
    print("Uploading screenshot...")
    success = await c.upload_screenshot(data, cap.get_resolution_str())
    print("Upload success:", success)
    cap.close()

if __name__ == "__main__":
    asyncio.run(main())
