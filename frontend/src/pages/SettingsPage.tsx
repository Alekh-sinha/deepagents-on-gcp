import { useApp } from "../state/app";
import { useAuth } from "../state/auth";
import { PageShell } from "../components/ui/PageShell";
import { Icon } from "../components/ui/Icon";

const Section = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <section className="card mb-5 p-5"><h2 className="mb-3 text-[15px] font-semibold">{title}</h2>{children}</section>
);
const Row = ({ k, v }: { k: string; v: React.ReactNode }) => <div className="flex gap-4 py-1.5 text-[13.5px]"><dt className="w-44 shrink-0 text-muted">{k}</dt><dd className="min-w-0 flex-1 break-words">{v}</dd></div>;

export function SettingsPage() {
  const app = useApp();
  const auth = useAuth();
  const c = app.config;
  return (
    <PageShell title="Settings" subtitle="What privai is allowed to do is set by the deployment's policy. This page shows it; it cannot be changed from here.">
      <Section title="Account">
        <dl><Row k="Signed in as" v={app.user} /></dl>
        <button type="button" className="btn-outline mt-3" onClick={() => void auth.signOut()}><Icon name="logout" size={14} />Sign out</button>
      </Section>
      <Section title="Models">
        <dl><Row k="Default model" v={app.modelById(c.default_model)?.label ?? c.default_model} /><Row k="Helpers choose models" v={app.helperChoice ? "Yes, never above your selected model" : "No, helpers use your model"} /></dl>
        <ul className="mt-3 divide-y divide-line rounded-lg border border-line">
          {app.models.map((m) => (
            <li key={m.id} className="flex items-baseline gap-3 px-3 py-2 text-[13px]"><span className="w-6 text-faint">#{m.rank}</span><span className="w-52 font-medium">{m.label}</span><span className="flex-1 text-muted">{m.use_for}</span></li>
          ))}
        </ul>
      </Section>
      <Section title="What it may do">
        <dl>
          <Row k="Email" v={`only to ${(c.email?.allowed_recipients ?? []).join(", ") || "nobody"}, at most ${c.email?.max_per_day ?? "?"} a day`} />
          <Row k="GitHub" v={`branches starting with ${c.github?.branch_prefix ?? "agent/"}, never the default branch${(c.github?.allowed_repos ?? []).length ? `; repositories: ${c.github.allowed_repos.join(", ")}` : ""}`} />
          <Row k="Shell commands" v={c.approvals.shell_commands ? "ask first" : "run without asking"} />
          <Row k="Approvals expire after" v={`${Math.round(c.approvals.ttl_seconds / 60)} minutes`} />
          <Row k="Turns" v={`up to ${c.limits.max_turn_minutes} minutes; ${c.limits.max_concurrent_turns} at a time`} />
        </dl>
      </Section>
    </PageShell>
  );
}
