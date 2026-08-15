import {useEffect, useRef, useState} from "react";

export function usePoliteAnnouncement(message: string, minimumIntervalMs = 5_000): string {
  const [announcement, setAnnouncement] = useState(message);
  const lastAnnouncement = useRef(Date.now());
  const latestMessage = useRef(message);

  useEffect(() => {
    latestMessage.current = message;
    if (!message || message === announcement) return;
    if (!announcement) {
      lastAnnouncement.current = Date.now();
      setAnnouncement(message);
      return;
    }
    const remaining = Math.max(0, minimumIntervalMs - (Date.now() - lastAnnouncement.current));
    if (remaining === 0) {
      lastAnnouncement.current = Date.now();
      setAnnouncement(message);
      return;
    }
    const timer = setTimeout(() => {
      lastAnnouncement.current = Date.now();
      setAnnouncement(latestMessage.current);
    }, remaining);
    return () => clearTimeout(timer);
  }, [announcement, message, minimumIntervalMs]);

  return announcement;
}
