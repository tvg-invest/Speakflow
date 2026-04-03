/*
 * SpeakFlow launcher — execve to the embedded Python binary
 * inside the .app bundle so macOS Accessibility trust persists.
 */
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <mach-o/dyld.h>

extern char **environ;

int main() {
    char *home = getenv("HOME");
    if (!home) {
        fprintf(stderr, "HOME not set\n");
        return 1;
    }

    /* Resolve path to this executable (inside .app/Contents/MacOS/) */
    char exe[1024];
    uint32_t sz = sizeof(exe);
    if (_NSGetExecutablePath(exe, &sz) != 0) {
        fprintf(stderr, "Could not resolve executable path\n");
        return 1;
    }

    /* Build path to embedded python3 next to this binary */
    char *slash = strrchr(exe, '/');
    if (!slash) { return 1; }
    *(slash + 1) = '\0';  /* keep trailing slash */

    char python[1024];
    snprintf(python, sizeof(python), "%spython3", exe);

    char script[512];
    snprintf(script, sizeof(script), "%s/.speakflow/run.py", home);

    char dir[512];
    snprintf(dir, sizeof(dir), "%s/.speakflow", home);
    chdir(dir);

    char *argv[] = {"SpeakFlow", script, NULL};
    execve(python, argv, environ);

    /* Fallback: try venv python if embedded copy not found */
    snprintf(python, sizeof(python), "%s/.speakflow/venv/bin/python3", home);
    execve(python, argv, environ);

    perror("execve failed");
    return 1;
}
