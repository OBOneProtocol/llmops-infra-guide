package main

import (
	"encoding/json"
	"log"
	"net/http"
)

func main() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"status": "ok", "app": "go-app"})
	})
	log.Println("go-app listening on 8080")
	log.Fatal(http.ListenAndServe(":8080", nil))
}
