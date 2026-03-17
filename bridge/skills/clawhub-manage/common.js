/**
 * Shared utilities for clawhub-manage skill.
 */

/** Validate a ClawHub skill name — alphanumeric + hyphens only. */
function validateSkillName(name) {
  if (!name || typeof name !== "string") {
    throw new Error("Skill name is required.");
  }
  if (!/^[a-zA-Z][a-zA-Z0-9-]{0,63}$/.test(name)) {
    throw new Error(
      `Invalid skill name: "${name}" — must start with a letter and contain only letters, numbers, and hyphens (max 64 chars).`,
    );
  }
  return name.toLowerCase();
}

/** Path where ClawHub installs skills. */
const SKILLS_DIR = "/skills";

module.exports = { validateSkillName, SKILLS_DIR };
