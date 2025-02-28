import datetime

def get_fantasy_stats(team_name=None):
    current_year = datetime.datetime.now().year  # Get current year
    previous_year = current_year - 1  # Use last season if current isn't available

    for year in [current_year, previous_year]:  # Try current first, then fallback
        sleeper_url = f"https://api.sleeper.app/v1/stats/nfl/regular/{year}"
        response = requests.get(sleeper_url)

        if response.status_code == 200:
            stats = response.json()
            if team_name:
                filtered_stats = {player: data for player, data in stats.items() if team_name.lower() in player.lower()}
                return filtered_stats if filtered_stats else f"No fantasy stats found for {team_name} in {year}."
            return stats
        
    return "Fantasy stats are currently unavailable. Try again later."


import gradio as gr
import requests
from bs4 import BeautifulSoup

# List of NFL teams
NFL_TEAMS = [
    "Giants", "Cowboys", "Eagles", "Commanders", "49ers", "Seahawks", "Rams", "Cardinals",
    "Packers", "Bears", "Lions", "Vikings", "Saints", "Falcons", "Buccaneers", "Panthers",
    "Chiefs", "Broncos", "Raiders", "Chargers", "Bills", "Patriots", "Dolphins", "Jets",
    "Ravens", "Bengals", "Steelers", "Browns", "Colts", "Titans", "Jaguars", "Texans"
]

# Function to get live scores
def get_live_scores(team_name=None):
    url = "https://www.espn.com/nfl/scoreboard"
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, "html.parser")

        # Find teams and their scores
        teams = soup.find_all("div", class_="ScoreCell__TeamName")
        scores = soup.find_all("div", class_="ScoreCell__Score")

        if len(teams) != len(scores):
            return "Error: Mismatch between teams and scores."

        game_results = []
        for i in range(0, len(teams), 2):  # Each game has 2 teams
            team1 = teams[i].text.strip()
            score1 = scores[i].text.strip()
            team2 = teams[i + 1].text.strip()
            score2 = scores[i + 1].text.strip()

            result = f"{team1} {score1} - {team2} {score2}"
            if not team_name or team_name.lower() in [team1.lower(), team2.lower()]:
                game_results.append(result)

        return game_results if game_results else f"No live scores found for {team_name}."

    else:
        return f"Error {response.status_code}: Unable to fetch scores."

# Function to get news for a specific team
def get_nfl_news(team_name):
    url = f"https://www.espn.com/nfl/team/_/name/{team_name.lower()}"
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, "html.parser")
        news_headlines = soup.find_all("a", class_="AnchorLink")

        articles = [headline.text.strip() for headline in news_headlines[:5]]  # Get top 5 headlines

        return articles if articles else f"No recent news found for {team_name}."
    else:
        return f"Error {response.status_code}: Unable to fetch news."

# Detect team names from user input
def detect_team(user_input):
    for team in NFL_TEAMS:
        if team.lower() in user_input.lower():
            return team
    return None

# Chatbot response function
def nfl_chatbot(user_input):
    team_name = detect_team(user_input)

    if "score" in user_input.lower():
        return get_live_scores(team_name)
    elif "fantasy" in user_input.lower():
        return get_fantasy_stats(team_name)
    elif "news" in user_input.lower() and team_name:
        return get_nfl_news(team_name)
    else:
        return "I can provide live scores, team news, and fantasy stats. Try asking: 'What are the Giants' latest scores?' or 'Get me fantasy stats for the Chiefs'."

# Deploy chatbot using Gradio
iface = gr.Interface(
    fn=nfl_chatbot,
    inputs="text",
    outputs="text",
    title="🏈 NFL Chatbot",
    description="Ask about live NFL scores, team news, and fantasy stats!"
)

iface.launch()
