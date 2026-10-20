// Reference definition of the Railpowered template's single service, written
// for Railway's TypeScript IaC (`railway/iac`). The published template at
// https://railway.com/new/template/AGX0Mu is edited in Railway's template
// editor; keep the two in step (see .railway/README.md). Build and deploy
// settings also live in /railway.toml, which wins at deploy time.
//
// Also set in the template editor, because a template cannot know them ahead:
//   - a generated public HTTP domain on port 8080 (the dashboard);
//   - ADMIN_KEY defaulting to ${{secret(32)}}: a random 32-character key the
//     owner reads (or replaces) in the service's Variables.
import { defineRailway, github, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const world = volume("minecraft-data", {
    // Railway caps volume size by plan; a smaller plan gets its own maximum.
    sizeMB: 5120,
  });

  const minecraft = service("Minecraft", {
    source: github("rndaom/Railpowered"),
    // No start command: the image runs tini, then /server/start.sh.
    healthcheck: "/health",
    healthcheckTimeout: 300,
    // Players connect to the TCP proxy; the dashboard uses the HTTP domain.
    tcp: [25565],
    deploy: {
      requiredMountPath: "/server/data",
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 10,
      // Time between SIGTERM and SIGKILL, so Minecraft can save on redeploy.
      drainingSeconds: 60,
      // Two containers must never share the world volume.
      overlapSeconds: 0,
    },
    volumeMounts: {
      "/server/data": world,
    },
    env: {
      ADMIN_KEY: {
        value: "${{secret(32)}}",
        description:
          "Dashboard password, generated for you. Find it in the service's Variables after deploying, or replace it with your own long passphrase (20+ characters). Weak keys are refused. Anyone who has it controls your server.",
        isOptional: false,
      },
      PORT: {
        value: "8080",
        description: "Dashboard port. Keep 8080 so Railway's health check and HTTP domain reach the dashboard.",
      },
      MINECRAFT_VERSION: {
        value: "latest",
        description:
          "First deploy only: the Minecraft version to start with (latest, or a version such as 1.21.1). Change versions later from the dashboard.",
      },
      SERVER_TYPE: {
        value: "vanilla",
        description:
          "First deploy only: vanilla, fabric or forge (Forge needs MINECRAFT_VERSION set to 1.16.5, 1.12.2 or 1.7.10). Add modpacks or other versions later from the dashboard.",
      },
      AUTO_START: {
        value: "false",
        description:
          "true starts Minecraft when the container boots. false waits until a player joins or you press Start. The dashboard setting overrides this.",
      },
      IDLE_TIMEOUT: {
        value: "600",
        description:
          "Seconds with nobody online before Minecraft goes to sleep (600 = 10 minutes, 0 = never, at most 86400 = one day). The dashboard setting overrides this.",
      },
      MC_MAX_MEMORY: {
        value: "auto",
        description:
          "Java heap limit: auto (fits the container, at most 2G) or a size such as 3G. Keep it below the service's memory limit.",
      },
      MC_MIN_MEMORY: {
        value: "256M",
        description: "Java heap reserved at start, such as 256M or 1G. Must not exceed MC_MAX_MEMORY.",
      },
    },
  });

  return project("Railpowered", {
    resources: [minecraft, world],
  });
});
