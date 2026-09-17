import { useState } from "react";
import "./App.css";

export default function App() {
  const [count, setCount] = useState(0);

  return (
    <main>
      <h1>New app</h1>
      <p>This starter is empty on purpose. Describe what you want to build.</p>
      <button type="button" onClick={() => setCount((value) => value + 1)}>
        Count is {count}
      </button>
    </main>
  );
}
