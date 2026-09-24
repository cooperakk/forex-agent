import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./theme.css";

// The document element is set from here rather than from index.html because the
// same bundle is served three ways: by the FastAPI process, by a static host,
// and inside a published page whose <html> element the bundle does not own.
const root = document.documentElement;
root.setAttribute("lang", "fa");
root.setAttribute("dir", "rtl");

const el = document.getElementById("root");
if (el) createRoot(el).render(<React.StrictMode><App /></React.StrictMode>);
