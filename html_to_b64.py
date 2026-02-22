import base64
with open("panel.html", "rb") as f:
    print(base64.b64encode(f.read()).decode("ascii"))