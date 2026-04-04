/*
 * SpeakFlow launcher — exec to the EMBEDDED Python interpreter.
 *
 * Uses the python3 binary inside the .app bundle so that macOS
 * Accessibility trust (tied to the binary's code signature) persists
 * across restarts.
 */
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <dirent.h>

extern char **environ;

int main() {
    char *home = getenv("HOME");
    if (!home) {
        fprintf(stderr, "HOME not set\n");
        return 1;
    }

    char app_python[512];
    char script[512];
    char dir[512];
    char venv_python[512];

    snprintf(app_python, sizeof(app_python),
             "%s/Desktop/SpeakFlow.app/Contents/MacOS/python3", home);
    snprintf(venv_python, sizeof(venv_python),
             "%s/.speakflow/venv/bin/python3", home);
    snprintf(script, sizeof(script), "%s/.speakflow/run.py", home);
    snprintf(dir, sizeof(dir), "%s/.speakflow", home);

    /* Use embedded python3 if available, else fall back to venv. */
    char *python = (access(app_python, X_OK) == 0) ? app_python : venv_python;

    /* Set PYTHONPATH so the embedded binary finds venv packages. */
    char venv_lib[512];
    snprintf(venv_lib, sizeof(venv_lib), "%s/.speakflow/venv/lib", home);
    char site_packages[1024] = {0};

    DIR *d = opendir(venv_lib);
    if (d) {
        struct dirent *entry;
        while ((entry = readdir(d)) != NULL) {
            if (strncmp(entry->d_name, "python3", 7) == 0) {
                snprintf(site_packages, sizeof(site_packages),
                         "%s/%s/site-packages", venv_lib, entry->d_name);
                break;
            }
        }
        closedir(d);
    }

    if (site_packages[0]) {
        setenv("PYTHONPATH", site_packages, 1);
    }

    chdir(dir);
    char *argv[] = {"SpeakFlow", script, NULL};
    execve(python, argv, environ);
    perror("execve failed");
    return 1;
}
