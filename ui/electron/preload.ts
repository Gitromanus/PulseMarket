import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("pf", {
  info: () => ipcRenderer.invoke("app:info"),
  pickExcel: () => ipcRenderer.invoke("dialog:pick-excel"),
});
