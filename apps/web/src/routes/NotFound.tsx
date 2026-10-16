import { Link } from "react-router";
import { Empty } from "../components/ui";

export function NotFoundPage() {
  return (
    <Empty title="Page not found" icon="question">
      <p>This route does not exist. <Link to="/requests">Go to requests</Link>.</p>
    </Empty>
  );
}
