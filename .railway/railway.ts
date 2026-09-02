import { defineRailway, github, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const world = volume("minecraft-data", {
    sizeMB: 5120,
  });

  const minecraft = service("Minecraft", {
    source: github("rndaom/roundhouse"),
    start: "./start.sh",
    healthcheck: "/health",
    healthcheckTimeout: 300,
    volumeMounts: {
      "/server/data": world,
    },
    env: {
      MINECRAFT_VERSION: "latest",
      SERVER_TYPE: "vanilla",
      AUTO_START: "false",
      IDLE_TIMEOUT: "600",
      MC_MAX_MEMORY: "1G",
      MC_MIN_MEMORY: "512M",
    },
  });

  return project("Roundhouse", {
    resources: [minecraft, world],
  });
});
