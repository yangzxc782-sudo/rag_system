import AppShell from "@/components/AppShell";
import ServiceStatus from "@/components/ServiceStatus";
import { getApiHealth, getHealth, getServiceHealth } from "@/lib/api";

export default async function Home() {
  const [appHealth, apiHealth, serviceHealth] = await Promise.all([
    getHealth(),
    getApiHealth(),
    getServiceHealth(),
  ]);

  return (
    <AppShell>
      <ServiceStatus
        appHealth={appHealth}
        apiHealth={apiHealth}
        serviceHealth={serviceHealth}
      />
    </AppShell>
  );
}
