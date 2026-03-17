#!/usr/bin/env node
/**
 * Install a ClawHub community skill.
 * Usage: node install.js <skill_name>
 *
 * The skill is downloaded to disk. It will be loaded by OpenClaw on the
 * next session start (after idle timeout or new conversation).
 */
const { execFileSync } = require("child_process");
const { validateSkillName } = require("./common");

const skillName = validateSkillName(process.argv[2]);

try {
  const output = execFileSync(
    "clawhub",
    ["install", skillName, "--no-input", "--force"],
    { encoding: "utf-8", timeout: 60_000, stdio: ["pipe", "pipe", "pipe"] },
  );
  console.log(`Successfully installed skill: ${skillName}`);
  if (output.trim()) console.log(output.trim());
  console.log(
    "\nThe skill will be available after the next session restart or idle timeout.",
  );
} catch (err) {
  const stderr = err.stderr?.trim() || err.message;
  console.error(`Failed to install skill "${skillName}": ${stderr}`);
  process.exit(1);
}
