import { SectionPage } from '../components/SectionPage';

export function Journal() {
  return (
    <SectionPage
      name="journal"
      icon="journal"
      periods={false}
      searchable
    />
  );
}
