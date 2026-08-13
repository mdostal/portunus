export default function StatePill({ state }: { state: string }) {
  return <span className={`state-pill state-${state}`}>{state}</span>;
}
