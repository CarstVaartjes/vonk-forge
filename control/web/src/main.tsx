import {StrictMode} from "react"; import {createRoot} from "react-dom/client"; import {ApiClient} from "./api/client"; import {App} from "./app"; import {AuthProvider} from "./auth"; import "./styles.css";
const api = new ApiClient();
createRoot(document.getElementById("root")!).render(<StrictMode><AuthProvider api={api}><App api={api}/></AuthProvider></StrictMode>);
