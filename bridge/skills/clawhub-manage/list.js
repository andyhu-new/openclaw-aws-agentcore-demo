#!/usr/bin/env node
/**
 * List installed ClawHub skills.
 * Usage: node list.js
 */
const fs = require("fs");
const path = require("path");
const { SKILLS_DIR } = require("./common");

try {
  if (!fs.existsSync(SKILLS_DIR)) {
    console.log("No skills directory found. No ClawHub skills installed.");
    process.exit(0);
  }

  const entries = fs.readdirSync(SKILLS_DIR, { withFileTypes: true });
  const skills = entries
    .filter((e) => e.isDirectory())
    .filter((e) => {
      // Only list dirs that have a SKILL.md (valid ClawHub skills)
      return fs.existsSync(path.join(SKILLS_DIR, e.name, "SKILL.md"));
    })
    .map((e) => e.name)
    .sort();

  if (skills.length === 0) {
    console.log("No ClawHub skills installed.");
  } else {
    console.log(`Installed ClawHub skills (${skills.length}):\n`);
    for (const skill of skills) {
      console.log(`  - ${skill}`);
    }
  }
} catch (err) {
  console.error(`Failed to list skills: ${err.message}`);
  process.exit(1);
}
