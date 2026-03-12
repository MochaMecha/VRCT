import { invoke } from "@tauri-apps/api/core";
import { Command } from "@tauri-apps/plugin-shell";
import { useEffect, useRef } from "react";

import { useStdoutToPython } from "@useStdoutToPython";
import { useReceiveRoutes } from "@useReceiveRoutes";
import { store, useStore_SelectableFontFamilyList } from "@store";
import { arrayToObject } from "@utils";

import {
    useNotificationStatus,
} from "@logics_common";

export const StartPythonController = () => {
    const { asyncStartPython } = useStartPython();
    const hasRunRef = useRef(false);
    const { asyncFetchFonts } = useAsyncFetchFonts();

    useEffect(() => {
        if (!hasRunRef.current) {
            asyncStartPython().then(() => {
                startFeedingToWatchDogController();
                asyncFetchFonts();
            }).catch((err) => {
                console.error(err);
            });
        }
        return () => hasRunRef.current = true;
    }, []);

    return null;
};

const useStartPython = () => {
    const { receiveRoutes } = useReceiveRoutes();
    const { showNotification_Success, showNotification_Error } = useNotificationStatus();

    const asyncStartPython = async () => {
        const command = Command.sidecar("bin/VRCT-sidecar");
        command.on("error", error => console.error(`error: "${error}"`));
        command.stdout.on("data", (line) => {
            let parsed_data = "";
            try {
                parsed_data = JSON.parse(line);
                receiveRoutes(parsed_data);
            } catch (error) {
                console.log(error, line);
            }
        });
        command.stderr.on("data", line => {
            // ALSA, PulseAudio, JACK, and PipeWire dump harmless probe
            // warnings to stderr when PyAudio enumerates devices on Linux.
            // Only show the notification for genuine errors.
            const ignoredPatterns = [
                "ALSA lib",
                "Cannot open device",
                "Unknown PCM",
                "unable to open slave",
                "a]",           // e.g. "_snd_pcm_a52_open"
                "connect to server failed",  // JACK not running
                "jackd",
                "JackShmReadWritePtr",
                "Cannot connect to server",
                "connect(2) call to",       // JACK socket not found
                "/dev/shm/jack",
                "warnings.warn",
                "SyntaxWarning",
                "invalid escape sequence",
                "UserWarning",
                "FutureWarning",
                "DeprecationWarning",
            ];
            const isIgnored = ignoredPatterns.some(p => line.includes(p));
            if (isIgnored) {
                console.warn("stderr (suppressed):", line);
            } else {
                showNotification_Error(
                    `An error occurred. Please restart VRCT or contact the developers. The last line:${JSON.stringify(line)}`, { hide_duration: null }
                );
                console.error("stderr", line);
            }
        });
        const backend_subprocess = await command.spawn();
        store.backend_subprocess = backend_subprocess;
    };

    return { asyncStartPython };
};

const useAsyncFetchFonts = () => {
    const { updateSelectableFontFamilyList } = useStore_SelectableFontFamilyList();
    const asyncFetchFonts = async () => {
        try {
            let fonts = await invoke("get_font_list");
            fonts = fonts.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
            updateSelectableFontFamilyList(arrayToObject(fonts));
        } catch (error) {
            console.error("Error fetching fonts:", error);
        }
    };
    return { asyncFetchFonts };
};

const startFeedingToWatchDogController = () => {
    const { asyncStdoutToPython } = useStdoutToPython();
    setInterval(() => {
        asyncStdoutToPython("/run/feed_watchdog");
    }, 20000); // 20000ミリ秒 = 20秒
};