SELECT
  firstname,
  lastname,
  COALESCE(city, NULL) AS CITY,
  COALESCE(state, NULL) AS STATE
FROM PERSON
LEFT JOIN ADDRESS
  ON address.personid = person.personid