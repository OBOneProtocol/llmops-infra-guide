const express = require("express");

const app = express();
const port = process.env.PORT || 3000;

app.get("/", (req, res) => res.json({ status: "ok", app: "node-app" }));

app.listen(port, () => console.log(`node-app listening on ${port}`));
