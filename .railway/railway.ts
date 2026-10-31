import { defineRailway, github, project, service } from "railway/iac";

export default defineRailway(() => {
  const lucidTransformation = service("lucid-transformation", {
    source: github("anshu018/Polymarket", { branch: "main", commitSha: "7cfd7fd3711291eba3d8e16839c64c59976a5a9f", upstreamUrl: "https://github.com/anshu018/Polymarket" }),
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    replicas: { "iad": 1 },
  });
  const Polymarket = service("Polymarket", {
    source: github("anshu018/Polymarket", { branch: "main", commitSha: "61d5527018a5ed174095ee9eceaaf2c8bf0d84f9", upstreamUrl: "https://github.com/anshu018/Polymarket" }),
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    replicas: { "iad": 1 },
    networking: { privateNetworkEndpoint: "polymarket" },
  });

  return project("Polymarket..", {
    variables: { managed: false },
    resources: [lucidTransformation, Polymarket],
  });
});
